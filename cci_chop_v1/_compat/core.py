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
    keys="symbols leverage margin_mode allocation_usdt target_notional_usdt max_trade_risk_usdt min_account_equity_usdt margin_reserve_usdt daily_loss_pct weekly_loss_pct max_drawdown_pct max_consecutive_losses loss_pause_hours cooldown_hours min_stop_pct max_stop_pct max_chase_pct entry_slippage_bps exit_slippage_bps fee_budget_floor max_spread_pct max_mark_basis_pct max_adverse_funding_rate entry_window_seconds max_tick_gap_seconds poll_seconds frame_refresh_seconds heartbeat_minutes exit_mode max_holding_minutes target_r min_group_observations min_state_observations probability_margin signal_delay_seconds".split()
    if set(c)!=set(keys):raise SafetyError("unexpected/missing configuration keys")
    fixed={'symbols':list(SYMBOLS),'leverage':5,'margin_mode':'crossed','allocation_usdt':400.,'target_notional_usdt':2000.,'max_trade_risk_usdt':20.,'daily_loss_pct':4.,'weekly_loss_pct':8.,'max_drawdown_pct':15.,'max_consecutive_losses':3,'loss_pause_hours':1.,'cooldown_hours':5/60,'min_stop_pct':.2,'max_stop_pct':.8,'max_chase_pct':.1,'exit_mode':'fixed_2r_60m','max_holding_minutes':60,'target_r':2.,'min_group_observations':300,'min_state_observations':80,'probability_margin':.02,'signal_delay_seconds':60,'margin_reserve_usdt':100.,'max_spread_pct':.02,'fee_budget_floor':.0006,'heartbeat_minutes':360}
    for k,v in fixed.items():
        if c[k]!=v:raise SafetyError('frozen release setting mismatch: '+k)
    for k in keys:
        if k not in ('symbols','margin_mode','exit_mode'):number(c[k],True)
    if c['min_account_equity_usdt']<910 or c['margin_reserve_usdt']<100:raise SafetyError('margin and fee reserve insufficient')
    if not .0006<=c['fee_budget_floor']<=.005 or c['entry_slippage_bps']>5 or c['exit_slippage_bps']>10:raise SafetyError('invalid cost settings')
    if c['max_spread_pct']>.02 or c['max_mark_basis_pct']>.25 or c['max_adverse_funding_rate']>.0005:raise SafetyError('invalid market limits')
    if not .5<=c['poll_seconds']<=3 or not 1<=c['frame_refresh_seconds']<=10 or not 5<=c['entry_window_seconds']<=30 or not 3<=c['max_tick_gap_seconds']<=15:raise SafetyError('invalid timing')
    return c

def fingerprint(root):
    root=Path(root);h=hashlib.sha256()
    for p in [root/'config.json',root/'probability_strategy.py',root/'deployment_model.json',*sorted((root/'basic_core').glob('*.py'))]:
        h.update(p.name.encode())
        if p==root/'deployment_model.json' and not p.exists():h.update(b'MISSING_MODEL_ENTRIES_DISABLED')
        else:h.update(p.read_bytes())
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
        for key in ('limitOpenTime','maintainTime','offTime'):
            if r.get(key) not in (None,'','0',0,'-1',-1) and number(r[key],True)<=now_ms():raise SafetyError('INSTRUMENT_ENTRY_DISABLED:'+key)
        if not number(r.get('minLeverage',1),True)<=5<=number(r.get('maxLeverage'),True):raise SafetyError("5x unsupported")
        maxima=[number(r[k]) for k in ('maxOrderQty','maxMarketOrderQty') if r.get(k) is not None and number(r[k])>0]
        i=cls(r['symbol'],number(r['priceMultiplier'],True),number(r['quantityMultiplier'],True),number(r['minOrderQty'],True),number(r['minOrderAmount'],True),min(maxima) if maxima else 1e12,number(r['takerFeeRate']))
        if not 0<=i.fee<.01:raise DataError("invalid fee")
        return i
