from __future__ import annotations
from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING
from datetime import datetime, timezone
from pathlib import Path
import hashlib, json, math, os, tempfile, time

SYMBOLS = ("BTCUSDT", "ETHUSDT")
CAT = "USDT-FUTURES"
H4 = 14_400_000
DAY = 86_400_000

class SafetyError(RuntimeError): pass
class DataError(SafetyError): pass
class Rejected(SafetyError): pass
class UnknownOrder(SafetyError): pass

def number(value, positive=False):
    try: n = float(value)
    except (ValueError, TypeError): raise DataError("missing/invalid number") from None
    if not math.isfinite(n) or (positive and n <= 0):
        raise DataError("non-finite/nonpositive number")
    return n

def rounded(value, step, up=False):
    v, s = Decimal(str(value)), Decimal(str(step))
    if not v.is_finite() or not s.is_finite() or s <= 0: raise DataError("invalid increment")
    return float((v/s).to_integral_value(rounding=ROUND_CEILING if up else ROUND_FLOOR)*s)

def ds(value): return format(Decimal(str(value)), "f")
def utc(ms): return datetime.fromtimestamp(ms/1000, timezone.utc).isoformat()
def now_ms(): return int(time.time()*1000)

def atomic(path, data):
    path=Path(path);path.parent.mkdir(parents=True, exist_ok=True)
    fd, name=tempfile.mkstemp(dir=path.parent, prefix=path.name+".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, allow_nan=False, indent=2)
            f.flush();os.fsync(f.fileno())
        os.chmod(name, 0o600);os.replace(name, path)
        d=os.open(str(path.parent), os.O_DIRECTORY)
        try: os.fsync(d)
        finally: os.close(d)
    finally:
        if os.path.exists(name): os.unlink(name)

def config(root):
    c=json.loads((Path(root)/"config.json").read_text())
    if c.get("symbols") != list(SYMBOLS) or c.get("leverage") != 5 or c.get("margin_mode") != "crossed":
        raise SafetyError("contract: BTCUSDT + ETHUSDT / crossed / 5x")
    keys='symbols leverage margin_mode risk_pct max_margin_pct daily_loss_pct weekly_loss_pct max_drawdown_pct max_consecutive_losses loss_pause_hours entry_channel exit_channel daily_fast daily_slow atr_period initial_structure_bars min_stop_atr max_stop_atr stop_buffer_atr min_stop_pct max_stop_pct max_cost_to_risk entry_slippage_bps exit_slippage_bps fee_budget_floor max_spread_pct max_mark_basis_pct max_chase_atr max_adverse_funding_rate entry_window_seconds cooldown_hours poll_seconds frame_refresh_seconds heartbeat_minutes paper_seed'.split()
    if set(c)!=set(keys): raise SafetyError("unexpected/missing configuration keys")
    for k,v in c.items():
        if k not in ("symbols","margin_mode"): number(v, positive=True)
    if not 0<c['risk_pct']<=0.5 or not 0<c['max_margin_pct']<=30:
        raise SafetyError("release risk cap: 0.5% risk / 30% margin")
    if not c['risk_pct']<=c['daily_loss_pct']<=3 or not c['daily_loss_pct']<=c['weekly_loss_pct']<=6 or not c['weekly_loss_pct']<=c['max_drawdown_pct']<=10:
        raise SafetyError("invalid daily/weekly/drawdown limits")
    for k in ('entry_channel','exit_channel','daily_fast','daily_slow','atr_period','initial_structure_bars','max_consecutive_losses'):
        if int(c[k])!=c[k] or not 2<=c[k]<=120: raise SafetyError("invalid integer parameter: "+k)
    if c['daily_fast']>=c['daily_slow'] or c['exit_channel']>=c['entry_channel']:
        raise SafetyError("invalid channel/trend periods")
    if not c['min_stop_pct']<c['max_stop_pct']<=10 or not c['min_stop_atr']<c['max_stop_atr']<=10:
        raise SafetyError("invalid stop range")
    if c['fee_budget_floor']>0.005 or c['max_cost_to_risk']>0.3 or c['entry_slippage_bps']>10 or c['exit_slippage_bps']>30:
        raise SafetyError("invalid cost budget")
    if not 1<=c['poll_seconds']<=10 or not 20<=c['frame_refresh_seconds']<=120 or c['entry_window_seconds']>900:
        raise SafetyError("invalid data timing")
    if c['max_spread_pct']>.1 or c['max_mark_basis_pct']>.5 or c['max_adverse_funding_rate']>.002 or c['max_chase_atr']>1:
        raise SafetyError('invalid execution filters')
    if c['loss_pause_hours']<24 or c['cooldown_hours']<4 or c['max_consecutive_losses']>3:
        raise SafetyError('release pause limits cannot be relaxed')
    return c

def fingerprint(root):
    root=Path(root);h=hashlib.sha256()
    for p in [root/'config.json',*sorted((root/'trend_core').glob('*.py'))]:
        h.update(p.name.encode());h.update(p.read_bytes())
    return h.hexdigest()

@dataclass(frozen=True)
class Bar:
    ts:int;open:float;high:float;low:float;close:float;volume:float;turnover:float
    @classmethod
    def parse(cls, r):
        if not isinstance(r,(list,tuple)) or len(r)<7: raise DataError("incomplete candle")
        b=cls(int(r[0]),*[number(x) for x in r[1:7]])
        if min(b.open,b.high,b.low,b.close)<=0 or b.high<max(b.open,b.close,b.low) or b.low>min(b.open,b.close) or min(b.volume,b.turnover)<0:
            raise DataError("invalid OHLCV")
        return b

@dataclass(frozen=True)
class Quote:
    symbol:str;ts:int;last:float;bid:float;ask:float;mark:float;index:float;funding:float
    def entry(self,side): return self.ask if side=='LONG' else self.bid
    def exit(self,side): return self.bid if side=='LONG' else self.ask
    def validate(self,now):
        if self.symbol not in SYMBOLS or not -2000<=now-self.ts<=8000: raise DataError("stale quote")
        for n in (self.last,self.bid,self.ask,self.mark,self.index):number(n,True)
        number(self.funding)
        if self.ask<self.bid:raise DataError("crossed order book")
    @property
    def spread_pct(self):return (self.ask-self.bid)/((self.ask+self.bid)/2)*100

@dataclass(frozen=True)
class Instrument:
    symbol:str;price_step:float;qty_step:float;min_qty:float;min_value:float;max_qty:float;fee:float
    @classmethod
    def parse(cls,r):
        if r.get('symbol') not in SYMBOLS or str(r.get('status')).lower()!='online' or str(r.get('symbolType','crypto')).lower()!='crypto':
            raise DataError("instrument unavailable/not crypto")
        if str(r.get('isReality','no')).lower()=='yes':raise DataError("unexpected instrument")
        if not number(r.get('minLeverage',1),True)<=5<=number(r.get('maxLeverage'),True):raise SafetyError("5x unsupported")
        maxima=[number(r[k]) for k in ('maxOrderQty','maxMarketOrderQty') if r.get(k) is not None and number(r[k])>0]
        i=cls(r['symbol'],number(r['priceMultiplier'],True),number(r['quantityMultiplier'],True),number(r['minOrderQty'],True),number(r['minOrderAmount'],True),min(maxima) if maxima else 1e12,number(r['takerFeeRate']))
        if not 0<=i.fee<.01:raise DataError("invalid fee")
        return i
