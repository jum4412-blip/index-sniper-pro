"""Closed-bar Donchian breakout, daily direction, structural exits. No fitted scores."""
from dataclasses import dataclass, asdict
from .core import *

def completed(rows,span,now,minimum):
    unique={}
    for b in rows:
        if b.ts+span>now-5000:continue
        if b.ts in unique and unique[b.ts]!=b:raise DataError('conflicting completed candles')
        unique[b.ts]=b
    a=sorted(unique.values(),key=lambda b:b.ts)
    if len(a)<minimum:raise DataError("candle warmup incomplete")
    # Use the exchange's actual session boundary. A daily candle may start at
    # 16:00 UTC; assuming UTC midnight would either reject it or mis-time closure.
    if any(b.ts%span!=a[0].ts%span for b in a):raise DataError('inconsistent candle session boundary')
    offset=a[0].ts%span
    expected_end=((now-5000-offset)//span)*span+offset
    if a[-1].ts+span!=expected_end:raise DataError('latest completed candle missing')
    if any(b.ts-a.ts!=span for a,b in zip(a,a[1:])):raise DataError("candle gap")
    return a

def ema(values,n):
    v=values[0];alpha=2/(n+1)
    for x in values[1:]:v+=alpha*(x-v)
    return v

def atr(rows,n):
    if len(rows)<n+1:raise DataError("ATR warmup")
    tr=[max(b.high-b.low,abs(b.high-a.close),abs(b.low-a.close)) for a,b in zip(rows,rows[1:])]
    value=sum(tr[-n:])/n
    return number(value,True)

@dataclass(frozen=True)
class Frame:
    symbol:str;built:int;bar_end:int;daily_end:int;direction:int;atr:float
    upper:float;lower:float;close:float;previous_close:float
    previous_upper:float;previous_lower:float;initial_low:float;initial_high:float
    exit_low:float;exit_high:float
    def asdict(self):return asdict(self)

def frame(symbol,daily,h4,c,now):
    d=completed(daily,DAY,now,max(180,c['daily_slow']*3))
    h=completed(h4,H4,now,max(80,c['entry_channel']+2))
    close=[b.close for b in d]
    fast=ema(close,c['daily_fast']);slow=ema(close,c['daily_slow'])
    direction=1 if close[-1]>fast>slow else -1 if close[-1]<fast<slow else 0
    n=c['entry_channel'];a=atr(h,c['atr_period']);s=c['initial_structure_bars'];x=c['exit_channel']
    return Frame(symbol,now,h[-1].ts+H4,d[-1].ts+DAY,direction,a,
                 max(b.high for b in h[-n-1:-1]),min(b.low for b in h[-n-1:-1]),h[-1].close,h[-2].close,
                 max(b.high for b in h[-n-2:-2]),min(b.low for b in h[-n-2:-2]),
                 min(b.low for b in h[-s:]),max(b.high for b in h[-s:]),
                 min(b.low for b in h[-x:]),max(b.high for b in h[-x:]))

def signal(f,c,now):
    if not 5000<=now-f.bar_end<=c['entry_window_seconds']*1000:return None
    if f.direction==1 and f.close>f.upper and f.previous_close<=f.previous_upper:
        return {'key':f'{f.symbol}:{f.bar_end}:LONG','symbol':f.symbol,'side':'LONG','trigger':f.upper,'bar_end':f.bar_end,'frame':f.asdict()}
    if f.direction==-1 and f.close<f.lower and f.previous_close>=f.previous_lower:
        return {'key':f'{f.symbol}:{f.bar_end}:SHORT','symbol':f.symbol,'side':'SHORT','trigger':f.lower,'bar_end':f.bar_end,'frame':f.asdict()}
    return None

def size(sig,q,inst,c,equity,now):
    q.validate(now);equity=number(equity,True)
    if q.symbol!=sig['symbol'] or inst.symbol!=q.symbol:raise SafetyError("symbol mismatch")
    if not 5000<=now-sig['bar_end']<=c['entry_window_seconds']*1000:raise SafetyError("expired/future signal")
    if now-sig['frame']['built']>180000:raise DataError('stale frame')
    f=sig['frame'];sign=1 if sig['side']=='LONG' else -1;entry=q.entry(sig['side']);a=f['atr']
    if q.spread_pct>c['max_spread_pct']:raise SafetyError("SPREAD_LIMIT")
    if abs(q.mark-q.index)/q.index*100>c['max_mark_basis_pct']:raise SafetyError("MARK_BASIS_LIMIT")
    if sign*q.funding>c['max_adverse_funding_rate']:raise SafetyError("FUNDING_LIMIT")
    if sign*(entry-sig['trigger'])<0:raise SafetyError("BREAKOUT_LOST")
    if abs(entry-f['close'])>c['max_chase_atr']*a:raise SafetyError("CHASE_LIMIT")
    min_gap=max(c['min_stop_atr']*a,entry*c['min_stop_pct']/100)
    raw_stop=min(f['initial_low']-c['stop_buffer_atr']*a,entry-min_gap) if sign==1 else max(f['initial_high']+c['stop_buffer_atr']*a,entry+min_gap)
    stop=rounded(raw_stop,inst.price_step,up=sign==-1)
    worst=entry*(1+sign*c['entry_slippage_bps']/10000)
    limit=rounded(worst,inst.price_step,up=sign==-1)
    if sign*(limit-entry)<0:raise SafetyError("entry spread smaller than allowed price step")
    gap=sign*(limit-stop)
    if stop<=0 or gap<=0 or sign*(q.mark-stop)<=inst.price_step:raise SafetyError("INVALID_STOP")
    if gap>c['max_stop_atr']*a or gap/entry*100>c['max_stop_pct']:raise SafetyError("STOP_TOO_WIDE")
    fee=max(inst.fee,c['fee_budget_floor'])
    # Allow an adverse exit beyond the stop. Entry is bounded by IOC price.
    exit_worst=stop*(1-sign*c['exit_slippage_bps']/10000)
    unit_cost=fee*(limit+exit_worst)+abs(exit_worst-stop)
    if unit_cost/gap>c['max_cost_to_risk']:raise SafetyError("COST_TO_RISK_LIMIT")
    unit_risk=gap+unit_cost
    raw=min(equity*c['risk_pct']/100/unit_risk,equity*c['max_margin_pct']/100*5/limit,inst.max_qty)
    qty=rounded(raw,inst.qty_step)
    if qty<inst.min_qty or qty*entry<inst.min_value:raise SafetyError("BELOW_EXCHANGE_MINIMUM")
    return {'qty':qty,'limit':limit,'stop':stop,'fee_rate':fee,'risk_budget':equity*c['risk_pct']/100,
            'estimated_risk':qty*unit_risk,'notional':qty*limit,'margin':qty*limit/5,'cost_to_risk':unit_cost/gap}

def manage(p,q,f,c,now):
    q.validate(now)
    if p['symbol']!=q.symbol:raise SafetyError("managed symbol mismatch")
    if f and f.symbol!=p['symbol']:raise SafetyError('managed frame symbol mismatch')
    sign=1 if p['side']=='LONG' else -1
    r=sign*(q.mark-p['entry'])/p['initial_r']
    p['mfe_r']=max(p.get('mfe_r',0),r);p['mae_r']=min(p.get('mae_r',0),r)
    if sign*(q.mark-p['stop'])<=0:return 'STRUCTURAL_STOP'
    # Only advance on newly completed 4H bars after entry. Never use a cost BE
    # level that can jump across the current market and immediately self-trigger.
    if f and now-f.built<=180000 and f.bar_end>max(p['opened'],p.get('exit_bar',0)):
        level=f.exit_low-c['stop_buffer_atr']*f.atr if sign==1 else f.exit_high+c['stop_buffer_atr']*f.atr
        level=rounded(level,p['price_step'],up=sign==-1)
        p['exit_bar']=f.bar_end
        if sign*(level-p['stop'])>0:
            if sign*(q.mark-level)<=0:return 'CHANNEL_EXIT'
            p['stop']=level
    return None
