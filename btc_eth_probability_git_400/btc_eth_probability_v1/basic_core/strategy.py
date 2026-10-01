"""Live adapter for the very same frozen causal events and probability lookup.

Only completed UTC five-minute price/base-volume bars feed probability_strategy.
A qualified event permits its first entry one minute after bar close. Existing
positions ignore new events; native initial stop, local fixed 2R and 60m exits.
"""
from dataclasses import dataclass,asdict
from pathlib import Path
import json
import numpy as np
import probability_strategy as probability
from .core import DataError,SafetyError,number,rounded
FIVE=300000
FRESH_MS=FIVE+90000
SETTLE_MS=2000

@dataclass(frozen=True)
class Frame:
    symbol:str;built:int;bar_end:int;direction:int;setup:str;event:dict|None;probability:dict;model_hash:str
    def asdict(self):return asdict(self)


def load_model(root,now):
    import hashlib
    path=Path(root)/'deployment_model.json'
    model=json.loads(path.read_text());probability.validate_model(model)
    cut=int(model['training_cut_ms'])
    if cut>now:raise SafetyError('PROBABILITY_MODEL_TRAINED_IN_FUTURE')
    if int(model.get('purge_ms',0))<3600000:raise SafetyError('MODEL_PURGE_BELOW_HOLDING_HORIZON')
    last=model.get('latest_label_end_ms')
    if last is not None and int(last)>=cut-int(model['purge_ms']):raise SafetyError('MODEL_LABEL_LEAKAGE')
    return model,hashlib.sha256(path.read_bytes()).hexdigest()


def frame(symbol,five,c,now,model,model_hash):
    a=np.array([[b.ts,b.open,b.high,b.low,b.close,b.volume,b.turnover] for b in five],dtype=float)
    try:
        a=probability.validate_candles(a)
        closed=a[a[:,0]+FIVE<=now-SETTLE_MS]
        if len(closed)<98:raise DataError('five-minute closed candle warmup incomplete')
        end=int(closed[-1,0])+FIVE
        if now-end>FIVE+90000:raise DataError('stale five-minute candles')
        event=probability.latest_event(symbol,closed,now_ms=now-SETTLE_MS)
        score=probability.predict_event(event,model) if event is not None else {'eligible':False,'reason':'NO_COMPLETED_PRICE_VOLUME_EVENT'}
        # Historical labels in older models were filled at a different order
        # size. Keep public monitoring available, but never trade a model whose
        # recorded training notional is absent or differs from live sizing.
        label_notional=model.get('label_notional_usdt')
        score={**score,'label_notional_usdt':label_notional}
        if label_notional!=c['target_notional_usdt']:
            score['eligible']=False
            score['reason']='MODEL_LABEL_NOTIONAL_MISMATCH'
    except (ValueError,KeyError,TypeError,OverflowError) as exc:raise DataError('PROBABILITY_FRAME_INVALID:'+type(exc).__name__) from None
    return Frame(symbol,now,end,int(event['side']) if event else 0,event['event_type'] if event else 'NONE',event,score,model_hash)


def _fresh(f,now):
    if not 0<=now-int(f['built'])<=FRESH_MS or not SETTLE_MS<=now-int(f['bar_end'])<=FRESH_MS:raise DataError('stale/future five-minute frame')


def score_eligible(score,c):
    if score.get('eligible') is not True:return False
    try:
        return (score.get('label_notional_usdt')==c['target_notional_usdt']
                and int(score['n_group'])>=c['min_group_observations'] and int(score['n_state'])>=c['min_state_observations']
                and int(score['days'])>=probability.SPEC.minimum_state_days
                and number(score['p05'])>=number(score['break_even'])+c['probability_margin']
                and number(score['lower95'])>0)
    except (KeyError,ValueError,TypeError,DataError):return False


class Crossings:
    """Name retained for durable runtime interface; these are timed bar events."""
    def __init__(self):self.queue={}
    def observe(self,f,q,c,now):
        q.validate(now);_fresh(f.asdict(),now)
        if f.symbol!=q.symbol:raise DataError('frame/quote symbol mismatch')
        event=f.event
        if not event or not score_eligible(f.probability,c):self.queue.pop(f.symbol,None);return None
        eligible_at=int(event['entry_after_ms'])
        if not 0<=now-eligible_at<=c['entry_window_seconds']*1000:self.queue.pop(f.symbol,None);return None
        side='LONG' if event['side']==1 else 'SHORT'
        sig={'key':f"{f.symbol}:{event['bar_close_ms']}:{event['side']}:{event['event_type']}:{f.model_hash}",
             'symbol':f.symbol,'side':side,'bar_end':event['bar_close_ms'],'crossed':eligible_at,
             'trigger':event['entry_reference'],'frame':f.asdict()}
        self.queue[f.symbol]=sig;return sig


def size(sig,q,inst,c,equity,now):
    q.validate(now);number(equity,True);f=sig['frame'];_fresh(f,now)
    if q.symbol!=sig['symbol'] or inst.symbol!=q.symbol or f['symbol']!=q.symbol:raise SafetyError('symbol mismatch')
    event=f.get('event')
    if not event or not score_eligible(f.get('probability',{}),c):raise SafetyError('PROBABILITY_EDGE_GATE_NOT_MET')
    eligible_at=int(event['entry_after_ms'])
    if not 0<=now-eligible_at<=c['entry_window_seconds']*1000:raise SafetyError('expired/future probability event')
    sign=1 if sig['side']=='LONG' else -1
    if event['side']!=sign or f['direction']!=sign or sig['trigger']!=event['entry_reference']:raise SafetyError('EVENT_DIRECTION_OR_REFERENCE_MISMATCH')
    if q.spread_pct>c['max_spread_pct']:raise SafetyError('SPREAD_LIMIT')
    if abs(q.mark-q.index)/q.index*100>c['max_mark_basis_pct']:raise SafetyError('MARK_BASIS_LIMIT')
    if sign*q.funding>c['max_adverse_funding_rate']:raise SafetyError('FUNDING_LIMIT')
    entry=q.entry(sig['side']);reference=number(event['entry_reference'],True)
    if abs(entry-reference)/reference*100>c['max_chase_pct']:raise SafetyError('CHASE_LIMIT')
    stop=rounded(number(event['stop'],True),inst.price_step,up=sign==-1)
    limit=rounded(entry*(1+sign*c['entry_slippage_bps']/10000),inst.price_step,up=sign==-1)
    if sign*(limit-entry)<0:raise SafetyError('PRICE_TICK_EXCEEDS_SLIPPAGE')
    gap=sign*(limit-stop)
    if stop<=0 or gap<=0 or sign*(q.mark-stop)<=inst.price_step:raise SafetyError('INVALID_STOP')
    stop_pct=gap/entry*100
    if stop_pct<c['min_stop_pct']:raise SafetyError('STOP_TOO_NARROW')
    if stop_pct>c['max_stop_pct']:raise SafetyError('STOP_TOO_WIDE')
    if inst.fee>c['fee_budget_floor']+1e-12:raise SafetyError('CURRENT_FEES_ABOVE_CALIBRATED_MODEL')
    fee=max(inst.fee,c['fee_budget_floor']);worst_exit=stop*(1-sign*c['exit_slippage_bps']/10000)
    unit_cost=fee*(limit+worst_exit)+abs(worst_exit-stop)
    sizing_price=max(limit,entry);qty=rounded(c['target_notional_usdt']/sizing_price,inst.qty_step)
    if qty>inst.max_qty:raise SafetyError('EXCHANGE_MAXIMUM_BELOW_TARGET_SIZE')
    if qty<inst.min_qty or qty*entry<inst.min_value:raise SafetyError('BELOW_EXCHANGE_MINIMUM')
    estimated_risk=qty*(gap+unit_cost);budget=c['max_trade_risk_usdt']
    if estimated_risk>budget:raise SafetyError('STRUCTURAL_RISK_LIMIT')
    notional=qty*sizing_price
    return {'qty':qty,'limit':limit,'stop':stop,'fee_rate':fee,'risk_budget':budget,'estimated_risk':estimated_risk,
            'notional':notional,'margin':notional/c['leverage'],'cost_to_risk':unit_cost/gap,'sizing_price':sizing_price}


def manage(p,q,f,c,now):
    q.validate(now)
    if p['symbol']!=q.symbol:raise SafetyError('managed symbol mismatch')
    sign=1 if p['side']=='LONG' else -1
    if sign*(q.mark-p['initial_stop'])<=0:return 'STRUCTURAL_STOP'
    target=p.get('target',p['entry']+sign*2*abs(p['entry']-p['initial_stop']))
    if sign*(q.exit(p['side'])-target)>=0:return 'FIXED_2R_TARGET'
    if now>=p.get('expires',p['opened']+int(c['max_holding_minutes']*60000)):return 'MAX_HOLDING_60_MINUTES'
    return None
