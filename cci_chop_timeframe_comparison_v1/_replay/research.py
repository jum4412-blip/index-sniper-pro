"""Causal, fixed-rule multi-timeframe structure replay and conservative calibration.

No exchange client or order writer is imported.  Separately held historical data are
hash-checked Binance USD-M proxies, not Bitget executions.  2025 onward was
already viewed and is explicitly a diagnostic, never an untouched holdout.
Only the frozen 2023-24 independent per-symbol trades enter calibration.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

MIN = 60_000
M5 = 5 * MIN
HOUR = 60 * MIN
H4 = 4 * HOUR
DAY = 24 * HOUR
WEEK = 7 * DAY
WEEK_ORIGIN = 345600000
TRAIN_START = 1672531200000
TRAIN_END = 1735689600000
ARCHIVE_END = 1790002800000
ACCOUNT_SNAPSHOT = 1042.19
MARGIN_CAP = 300.0
RISK_FRACTION = .01
RISK_BUDGET_CAP = 12.0
PORTFOLIO_RISK_FRACTION = .02
RESERVE = 150.0
LEVERAGES = (1, 2, 3, 5)
FULL_SPREAD_BPS = 3.0
SIDE_IMPACT_BPS = 7.0
MIN_CELL_TRADES = 100
MIN_ACTIVE_WEEKS = 20
BOOTSTRAP_REPETITIONS = 2000
CALIBRATION_CELL_FAMILY = 8
CALIBRATION_LOWER_ALPHA = .05/CALIBRATION_CELL_FAMILY
PROXY_HASHES = {
    'BTCUSDT_minute.npz': '9b75ba34d34c173806ecebb4970ce16358a7a70c77a6319b0f914ba5c7266be9',
    'BTCUSDT_funding.npz': '2e63badc8d0f7cf68828b6ce5328c6cf2971d66c6e64cb74c55815d1eccb9df3',
    'ETHUSDT_minute.npz': 'a71223e1bb57c634b2626bf531dd48df4726eff6a136bd4dd2c473b7b46bcb30',
    'ETHUSDT_funding.npz': '0d93f8937f3909573471217ea2cefec544d7b21e80dda8a7f11718198b3a204e',
}
PROXY_INSTRUMENT = {
    'BTCUSDT': {'tick': .1, 'qty_step': .0001, 'taker_fee': .0006, 'personal_fee': .0004},
    'ETHUSDT': {'tick': .01, 'qty_step': .01, 'taker_fee': .0006, 'personal_fee': .0004},
}


@dataclass
class Market:
    symbol: str
    minute: object
    funding: object
    tick: float
    qty_step: float
    fee: float
    personal_fee: float
    source: str
    manifest: dict


def _file_sha256(path: Path) -> str:
    digest=hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda:f.read(4_194_304),b''):
            digest.update(chunk)
    return digest.hexdigest()


def validate_proxy_arrays(minute, funding) -> None:
    """Same strict candle/rate checks as the verified original archive loader."""
    import numpy as np
    if minute.ndim != 2 or minute.shape[1] != 7 or not len(minute):
        raise ValueError('1m bars must be nonempty [open_ms,open,high,low,close,base_volume,usdt_turnover]')
    if not np.isfinite(minute).all() or not np.all(minute[:,0]==np.floor(minute[:,0])):
        raise ValueError('non-finite or non-integral 1m bars')
    if int(minute[0,0]) % HOUR or np.any(np.diff(minute[:,0]) != MIN):
        raise ValueError('timestamp gap, duplicate, or non-hour-aligned beginning')
    op, high, low, close, volume, turnover=(minute[:,i] for i in range(1,7))
    if (np.any(low <= 0) or np.any(op <= 0) or np.any(close <= 0) or
            np.any(high < np.maximum(op,close)) or np.any(low > np.minimum(op,close)) or
            np.any(volume < 0) or np.any(turnover < 0)):
        raise ValueError('invalid candle or turnover')
    if funding.ndim != 2 or funding.shape[1] != 2 or not len(funding):
        raise ValueError('funding events required; do not silently assume zero funding')
    if not np.isfinite(funding).all() or np.any(np.diff(funding[:,0]) <= 0):
        raise ValueError('invalid or duplicated funding timestamps')
    if np.any(np.abs(funding[:,1]) > .05):
        raise ValueError('implausible funding; verify units and source')


def load_proxy(cache: Path, symbol: str) -> Market:
    """Load the separately held exact NPZ archive; never fetch or synthesize it."""
    import numpy as np
    if symbol not in PROXY_INSTRUMENT:
        raise ValueError('No verified proxy for this symbol')
    paths=[cache/f'{symbol}_{kind}.npz' for kind in ('minute','funding')]
    for path in paths:
        if _file_sha256(path) != PROXY_HASHES[path.name]:
            raise ValueError(f'proxy digest mismatch: {path.name}')
    with np.load(paths[0],allow_pickle=False) as archive:
        minute=archive['bars']
    with np.load(paths[1],allow_pickle=False) as archive:
        funding=archive['events']
    validate_proxy_arrays(minute,funding)
    instrument=PROXY_INSTRUMENT[symbol]
    return Market(symbol,minute,funding,instrument['tick'],instrument['qty_step'],
                  instrument['taker_fee'],instrument['personal_fee'],
                  'Binance USD-M 1m archive (cross-exchange proxy)',
                  {'minute_sha256':PROXY_HASHES[paths[0].name],
                   'funding_sha256':PROXY_HASHES[paths[1].name],
                   'fee_provenance':'Bitget 2026-10-01 public instrument takerFeeRate',
                   'fee_personalized':'User-provided 2026-10-01 account snapshot; sensitivity assumption, not historical fee proof'})


def utc(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat()


def state_key(symbol: str, side: str, volume_ratio: float) -> str:
    if side not in ('long', 'short') or not math.isfinite(volume_ratio) or volume_ratio < 1.5:
        raise ValueError('invalid calibrated signal state')
    bucket = 'high_volume' if volume_ratio >= 2.0 else 'confirmed_volume'
    return f'{symbol}|{side}|{bucket}'


def probability_gate(model: dict, symbol: str, side: str, volume_ratio: float,
                     expected_spec_sha256: str) -> dict:
    """Read a frozen model without numeric packages or exchange side effects.

    Statistical cells never override model-wide source/execution restrictions.
    A Binance proxy alone cannot authorize an order on Bitget.
    """
    if not isinstance(model, dict) or type(model.get('schema_version')) is not int or model.get('schema_version') != 1:
        return {'eligible': False, 'reason': 'MODEL_SCHEMA_INVALID'}
    if model.get('strategy_sha256') != expected_spec_sha256:
        return {'eligible': False, 'reason': 'MODEL_STRATEGY_MISMATCH'}
    try:
        key = state_key(symbol, side, volume_ratio)
    except (ValueError, TypeError):
        return {'eligible': False, 'reason': 'INVALID_SIGNAL_STATE'}
    if not isinstance(model.get('cells'),dict):
        return {'eligible': False, 'reason': 'MODEL_CELLS_INVALID'}
    cell = model['cells'].get(key)
    if not isinstance(cell, dict):
        return {'eligible': False, 'reason': 'UNSEEN_STATE', 'state': key}
    if cell.get('statistical_gate_passed') is not True:
        return {'eligible': False, 'reason': 'NET_EDGE_OR_SAMPLE_GATE_FAILED', 'state': key,
                'trade_count': cell.get('trade_count'),
                'net_win_probability': cell.get('net_win_probability'),
                'failure_reasons': cell.get('failure_reasons', [])}
    def integer_at_least(value,minimum):
        return type(value) is int and value >= minimum
    def finite_number(value):
        return type(value) in (int,float) and math.isfinite(value)
    probability=cell.get('net_win_probability')
    lower=cell.get('wilson_95pct_one_sided_lower')
    numerical_ok=(integer_at_least(cell.get('trade_count'),MIN_CELL_TRADES) and
                  integer_at_least(cell.get('active_week_blocks'),MIN_ACTIVE_WEEKS) and
                  finite_number(probability) and 0 <= probability <= 1 and
                  finite_number(lower) and 0 <= lower <= probability and
                  finite_number(cell.get('average_net_bps')) and cell['average_net_bps'] > 0)
    for field in ('day_base_lower_bps','week_base_lower_bps',
                  'day_double_cost_lower_bps','week_double_cost_lower_bps'):
        numerical_ok = numerical_ok and finite_number(cell.get(field)) and cell[field] > 0
    blocks=cell.get('chronological_four_subperiods')
    numerical_ok = numerical_ok and isinstance(blocks,list) and len(blocks)==4
    if numerical_ok:
        for block in blocks:
            if (not isinstance(block,dict) or not integer_at_least(block.get('trades'),15) or
                    not finite_number(block.get('sum_net_bps')) or block['sum_net_bps'] <= 0):
                numerical_ok=False
                break
    if not numerical_ok or cell.get('failure_reasons') != []:
        return {'eligible': False, 'reason': 'MODEL_CELL_EVIDENCE_INVALID', 'state': key}
    if model.get('training_end_exclusive_ms') != TRAIN_END or type(model.get('training_end_exclusive_ms')) is not int:
        return {'eligible': False, 'reason': 'MODEL_TRAINING_CUTOFF_INVALID', 'state': key}
    if model.get('native_bitget_data_verified') is not True:
        return {'eligible': False, 'reason': 'NATIVE_BITGET_EVIDENCE_MISSING', 'state': key}
    if model.get('execution_and_protection_verified') is not True:
        return {'eligible': False, 'reason': 'EXECUTION_PROTECTION_EVIDENCE_MISSING', 'state': key}
    if model.get('prospective_validation_verified') is not True:
        return {'eligible': False, 'reason': 'PROSPECTIVE_VALIDATION_MISSING', 'state': key}
    if model.get('deployment_approved') is not True:
        return {'eligible': False, 'reason': 'DEPLOYMENT_NOT_APPROVED', 'state': key}
    return {'eligible': True, 'reason': 'FROZEN_NET_EDGE_PASSED', 'state': key,
            'net_win_probability': cell['net_win_probability'],
            'trade_count': cell['trade_count']}


def aggregate_frame(minute, width_ms: int, origin_ms: int = 0):
    """Aggregate complete UTC buckets only, trimming incomplete archive edges.

    Weekly buckets begin Monday 00:00 UTC.  No partial first/last bucket is
    promoted into a completed higher-timeframe candle.
    """
    import numpy as np
    if minute.ndim != 2 or minute.shape[1] != 7 or not len(minute):
        raise ValueError('1m OHLCV archive shape invalid')
    if width_ms < MIN or width_ms % MIN or not np.isfinite(minute).all():
        raise ValueError('invalid frame or non-finite archive')
    if np.any(np.diff(minute[:, 0]) != MIN):
        raise ValueError('1m archive gap/duplicate: replay refused')
    count = width_ms//MIN
    skip = int((-(int(minute[0,0])-origin_ms)) % width_ms//MIN)
    stop = skip+(len(minute)-skip)//count*count
    if stop <= skip:
        return np.empty((0,7),float)
    chunks = minute[skip:stop].reshape(-1,count,7)
    return np.column_stack((chunks[:,0,0],chunks[:,0,1],chunks[:,:,2].max(axis=1),
                            chunks[:,:,3].min(axis=1),chunks[:,-1,4],
                            chunks[:,:,5].sum(axis=1),chunks[:,:,6].sum(axis=1)))


def aggregate_hour(minute):
    return aggregate_frame(minute,HOUR)


FRAME_WIDTHS = {'W':WEEK,'D':DAY,'H4':H4,'H1':HOUR,'M5':M5}
FRAME_LOOKBACK = {'W':20,'D':90,'H4':120,'H1':72,'M5':64}


def prepare(market) -> dict:
    import numpy as np
    if len(market.funding) == 0 or np.any(np.diff(market.funding[:,0]) > 16*HOUR):
        raise ValueError('complete funding coverage required; no zero-funding fallback')
    if (market.funding[0,0] > market.minute[0,0] or
            market.funding[-1,0] < market.minute[-1,0]-16*HOUR):
        raise ValueError('funding does not cover archive interval')
    frames={name:aggregate_frame(market.minute,width,WEEK_ORIGIN if name=='W' else 0)
            for name,width in FRAME_WIDTHS.items()}
    return {'market':market,'frames':frames,'guard_cache':{},'macro_cache':{}}


def frame_candles(prepared: dict, asof_ms: int):
    """Exactly the completed and bounded histories supplied by the live runner."""
    import numpy as np
    from .strategy import Candle
    output={}
    for name,series in prepared['frames'].items():
        end=int(np.searchsorted(series[:,0],asof_ms-FRAME_WIDTHS[name],side='right'))
        output[name]=[Candle(int(row[0]),*map(float,row[1:6]))
                      for row in series[max(0,end-FRAME_LOOKBACK[name]):end]]
    output['tick_size']=prepared['market'].tick
    return output


def higher_context(prepared: dict, asof_ms: int) -> dict:
    """Cache higher-frame evidence at the latest completed H1 boundary.

    W/D/H4/H1 all roll on that boundary; M5 fields from this snapshot are
    deliberately ignored. The full strategy remains the final entry judge.
    """
    from .strategy import analyze
    stamp=asof_ms//HOUR*HOUR
    cache=prepared.setdefault('higher_context_cache',{})
    if stamp not in cache:
        cache[stamp]=analyze(frame_candles(prepared,stamp),stamp)
    return cache[stamp]


def _higher_entry_possible(context: dict, close: float, side: str) -> bool:
    errors=context['frame_errors']
    evidence=context.get('per_frame_evidence',{})
    if any(evidence.get(frame,{}).get('total_bars',0)<FRAME_LOOKBACK[frame]
           for frame in ('W','D','H4','H1')):
        return False
    if any(frame in errors for frame in ('W','D','H4','H1')):
        return False
    target=1 if side=='long' else -1
    if any(context['bias'][frame]['direction']!=target for frame in ('W','D')):
        return False
    opposite='low' if target==1 else 'high'
    valid=lambda price,level: target*(price-level)>0
    h4=context['structure']['H4'];p4=h4[opposite]
    h1=context['structure']['H1'];p1=h1[opposite+'s']
    if not p4 or len(p1)<2:
        return False
    return (valid(h4['close'],p4['price']) and valid(h1['close'],p1[-1]['price'])
            and valid(p1[-1]['price'],p1[-2]['price']) and valid(close,p4['price'])
            and valid(close,p1[-1]['price']))


def candidates(prepared: dict, start_ms: int, end_ms: int) -> list[dict]:
    """Run the frozen rule only after an exact necessary M5 prefilter.

    Prefiltering cannot create a signal: every surviving event still goes
    through the unmodified full multi-timeframe strategy.
    """
    import numpy as np
    from .strategy import generate_signal
    market=prepared['market']; bars=prepared['frames']['M5']
    if len(bars) <= 20:
        return []
    window=np.lib.stride_tricks.sliding_window_view
    prior_high=window(bars[:,2],20)[:-1].max(axis=1)
    prior_low=window(bars[:,3],20)[:-1].min(axis=1)
    prior_volume=np.median(window(bars[:,5],20)[:-1],axis=1)
    candidate=((bars[20:,4]>prior_high)|(bars[20:,4]<prior_low))
    candidate &= (prior_volume>0)&(bars[20:,5]>=1.5*prior_volume)
    times=bars[20:,0]+M5+MIN
    candidate &= (times>=start_ms)&(times<end_ms)
    out=[]
    for j in np.flatnonzero(candidate)+20:
        closed_ms=int(bars[j,0])+M5
        entry_ms=closed_ms+MIN
        side='long' if bars[j,4]>prior_high[j-20] else 'short'
        if not _higher_entry_possible(higher_context(prepared,closed_ms),float(bars[j,4]),side):
            continue
        frames=frame_candles(prepared,closed_ms)
        if any(len(frames[frame])<FRAME_LOOKBACK[frame] for frame in FRAME_LOOKBACK):
            continue
        signal=generate_signal(market.symbol,frames,closed_ms)
        if signal is None:
            continue
        i=(entry_ms-int(market.minute[0,0]))//MIN
        if 0 <= i < len(market.minute):
            out.append({'symbol':market.symbol,'side':signal.side,'guard':signal.guard,
                        'score':signal.score,'metadata':signal.metadata,
                        'event_id':signal.event_id,'m5_j':int(j),'entry_i':i,
                        'entry_ms':entry_ms,'entry_ref':float(market.minute[i,1])})
    return out


def management_update(prepared: dict, side: str, current_guard: float, asof_ms: int) -> dict:
    """Equivalent frozen update_guard using cached higher evidence + closed M5.

    The cache contains only completed W/D/H4/H1 evidence. M5 close, current
    guard and ratchet checks are evaluated afresh at every actual decision.
    Numerical and behavioral parity is tested against the pure strategy.
    """
    import numpy as np
    context=higher_context(prepared,asof_ms)
    target=1 if side=='long' else -1
    opposite='low' if side=='long' else 'high'
    reasons=[]
    for frame in ('W','D'):
        if context['bias'][frame]['direction']==-target:
            reasons.append(frame+'_BIAS_REVERSED')
    structure=context['structure'];p4=structure['H4'][opposite]
    if p4 and target*(structure['H4']['close']-p4['price'])<=0:
        reasons.append('H4_CONFIRMED_STRUCTURE_BREACHED')
    series=prepared['frames']['M5']
    j=int(np.searchsorted(series[:,0],asof_ms-M5,side='right'))-1
    if j<0 or int(series[j,0])+M5 != asof_ms//M5*M5:
        raise ValueError('closed M5 missing at management decision')
    price=float(series[j,4])
    pivots=structure['H1'][opposite+'s']
    candidate=current_guard
    if pivots:
        candidate=pivots[-1]['price']-target*prepared['market'].tick
        if candidate<=0:
            candidate=current_guard
    proposed=max(current_guard,candidate) if target==1 else min(current_guard,candidate)
    if target*(price-current_guard)<=0:
        reasons.append('M5_CLOSE_THROUGH_EXISTING_GUARD')
    elif target*(price-proposed)<=0:
        reasons.append('M5_CLOSE_THROUGH_PROPOSED_GUARD')
    return {'guard':current_guard if reasons else proposed,'should_exit':bool(reasons),
            'reason':reasons[0] if reasons else ('STRUCTURAL_GUARD_TIGHTENED' if proposed!=current_guard else 'STRUCTURAL_GUARD_UNCHANGED')}


def replay_path(prepared: dict, signal: dict, end_ms: int) -> dict:
    """Native guard first; completed MTF decisions act only 60s later.

    Any minute opening gap fills at its adverse open. A newly known H1 pivot
    cannot protect its confirming hours. Macro reversal and H4 structure exit
    follow the same delayed M5 decision as the runner. The last open position
    is an administrative mark excluded from completed calibration labels.
    """
    market=prepared['market'];minute=market.minute
    sign=1 if signal['side']=='long' else -1
    guard=float(signal['guard']);start_i=signal['entry_i']
    final_i=min(len(minute)-1,(end_ms-int(minute[0,0]))//MIN)
    last_decision=signal['entry_ms']-MIN
    for i in range(start_i,final_i+1):
        ts,op,high,low=map(float,minute[i,:4]);stamp=int(ts)
        if i==final_i:
            return {'exit_i':i,'exit_ms':stamp,'exit_ref':op,'last_guard':guard,
                    'reason':'administrative_period_end_mark','intraminute':False,'censored':True}
        if sign*(op-guard)<=0:
            return {'exit_i':i,'exit_ms':stamp,'exit_ref':op,'last_guard':guard,
                    'reason':'opening_gap_through_structure','intraminute':False,'censored':False}
        if stamp%M5==MIN and stamp-MIN>last_decision:
            update=management_update(prepared,signal['side'],guard,stamp-MIN)
            guard=float(update['guard']);last_decision=stamp-MIN
            if update['should_exit'] or sign*(op-guard)<=0:
                return {'exit_i':i,'exit_ms':stamp,'exit_ref':op,'last_guard':guard,
                        'reason':'completed_MTF_'+update['reason'],'intraminute':False,'censored':False}
        if sign*((low if sign==1 else high)-guard)<=0:
            return {'exit_i':i,'exit_ms':stamp,'exit_ref':guard,'last_guard':guard,
                    'reason':'exchange_guard_proxy_touch','intraminute':True,'censored':False}
    raise ValueError('empty replay range')


def funding_cash(market, entry_ms: int, path: dict, sign: int, qty: float,
                 multiplier: int) -> float:
    import numpy as np
    events = market.funding
    # Boundary ordering is unknown: include only adverse cash at entry/exit.
    a = int(np.searchsorted(events[:, 0], entry_ms, side='left'))
    end = path['exit_ms']+(MIN if path['intraminute'] else 0)
    b = int(np.searchsorted(events[:, 0], end, side='right' if not path['intraminute'] else 'left'))
    if a >= b:
        return 0.0
    observed = events[a:b]
    indices = np.searchsorted(market.minute[:, 0], observed[:, 0], side='right')-1
    if np.any(indices < 0) or np.any(indices >= len(market.minute)):
        raise ValueError('funding mark outside archive')
    cash = -sign*qty*observed[:, 1]*market.minute[indices, 1]
    ambiguous = (observed[:, 0] == entry_ms) | (observed[:, 0] >= path['exit_ms'])
    cash[ambiguous] = np.minimum(cash[ambiguous], 0.0)
    cash = np.where(cash < 0, cash*multiplier, cash)
    return float(cash.sum())


def _round_price(value: float, tick: float, up: bool) -> float:
    return (math.ceil(value/tick-1e-10) if up else math.floor(value/tick+1e-10))*tick


def materialize(prepared: dict, signal: dict, path: dict, leverage: int,
                multiplier: int, personalized: bool, equity: float,
                unit_notional: bool = False) -> dict | None:
    market = prepared['market']
    sign = 1 if signal['side'] == 'long' else -1
    if personalized and market.personal_fee is None:
        raise ValueError('per-symbol personalized fee not observed')
    rate = (market.personal_fee if personalized else market.fee)*multiplier
    friction = (FULL_SPREAD_BPS/2+SIDE_IMPACT_BPS)/10000*multiplier
    entry = _round_price(signal['entry_ref']*(1+sign*friction), market.tick, sign == 1)
    exit_price = _round_price(path['exit_ref']*(1-sign*friction), market.tick, sign == -1)
    guard_exit = _round_price(signal['guard']*(1-sign*friction),market.tick,sign == -1)
    risk_unit = sign*(entry-guard_exit)+rate*(entry+guard_exit)
    if sign*(entry-signal['guard']) <= 0 or risk_unit <= 0 or equity <= 0:
        return None
    if unit_notional:
        qty = 1.0/entry
    else:
        qty = min(MARGIN_CAP*leverage/entry, min(equity*RISK_FRACTION,RISK_BUDGET_CAP)/risk_unit)
        qty = math.floor(qty/market.qty_step+1e-10)*market.qty_step
    if qty <= 0:
        return None
    notional = qty*entry
    if not unit_notional and notional < 5.0:
        return None
    gross = sign*(exit_price-entry)*qty
    fees = rate*(entry+exit_price)*qty
    funding = funding_cash(market,signal['entry_ms'],path,sign,qty,multiplier)
    ratio = float(signal['metadata']['volume_ratio'])
    return {'symbol': signal['symbol'], 'side': signal['side'], 'event_id': signal['event_id'],
            'state': state_key(signal['symbol'], signal['side'], ratio),
            'entry_ms': signal['entry_ms'], 'exit_ms': path['exit_ms'],
            'entry_utc': utc(signal['entry_ms']), 'exit_utc': utc(path['exit_ms']),
            'entry': entry, 'exit': exit_price, 'qty': qty, 'notional_usdt': notional,
            'margin_usdt': notional/leverage, 'leverage': leverage,
            'initial_guard': signal['guard'], 'last_guard': path['last_guard'],
            'planned_guard_risk_usdt': qty*risk_unit, 'volume_ratio': ratio,
            'reason': path['reason'], 'censored': path['censored'],
            'gross_usdt': gross, 'fees_usdt': fees, 'funding_usdt': funding,
            'entry_fees_usdt': rate*entry*qty,
            'net_usdt': gross-fees+funding, 'net_bps': (gross-fees+funding)/notional*10000,
            'cost_multiplier': multiplier}


def independent_opportunities(prepared: dict, signals: list[dict], end_ms: int) -> list[tuple[dict, dict]]:
    """One position per symbol; overlapping opportunities do not become labels."""
    out = []
    next_entry = -1
    for signal in signals:
        if signal['entry_ms'] < next_entry:
            continue
        path = replay_path(prepared,signal,end_ms)
        out.append((signal,path))
        # A new entry in the same stopped minute has unknown event ordering.
        next_entry = path['exit_ms']+MIN
    return out


def run_portfolio(markets: dict, opportunities: dict, leverage: int, multiplier: int,
                  personalized: bool, initial_equity: float = ACCOUNT_SNAPSHOT) -> tuple[list[dict], dict]:
    pending, trades, rejected = [], [], Counter()
    realized = initial_equity
    merged = [pair for pairs in opportunities.values() for pair in pairs]
    merged.sort(key=lambda x:(x[0]['entry_ms'], -x[0]['score'], x[0]['symbol']))
    for signal,path in merged:
        now = signal['entry_ms']
        realized += sum(r['net_usdt'] for r in pending if r['exit_ms'] < now)
        pending = [r for r in pending if r['exit_ms'] >= now]
        if len(pending) >= 2 or any(r['symbol']==signal['symbol'] for r in pending):
            rejected['position_limit'] += 1
            continue
        mark_equity = realized
        for r in pending:
            m = markets[r['symbol']]['market']
            i = min(len(m.minute)-1,max(0,(now-int(m.minute[0,0]))//MIN))
            sign = 1 if r['side']=='long' else -1
            mark_equity += sign*(float(m.minute[i,1])-r['entry'])*r['qty']-r['entry_fees_usdt']
            mark_equity += funding_cash(m,r['entry_ms'],{'exit_ms':now,'intraminute':False},
                                        sign,r['qty'],multiplier)
        row = materialize(markets[signal['symbol']],signal,path,leverage,multiplier,
                          personalized,mark_equity)
        if row is None:
            rejected['size_or_risk'] += 1
            continue
        aggregate_risk=sum(r.get('planned_guard_risk_usdt',0.0) for r in pending)+row.get('planned_guard_risk_usdt',0.0)
        if aggregate_risk > mark_equity*PORTFOLIO_RISK_FRACTION+1e-9:
            rejected['aggregate_risk'] += 1
            continue
        occupied = sum(r['margin_usdt'] for r in pending)
        if mark_equity-occupied < row['margin_usdt']+RESERVE+1e-9:
            rejected['cash_reserve'] += 1
            continue
        trades.append(row)
        pending.append(row)
    return sorted(trades,key=lambda r:(r['exit_ms'],r['symbol'])),dict(rejected)


def grouped_lower(trades: list[dict], start_ms: int, end_ms: int,
                  unit: int, field: str, seed: int = 52731,
                  alpha: float = .05) -> float | None:
    """Whole day/week iid block resampling, fixed 5th-percentile lower total.

    This is a finite diagnostic, not a guarantee or an independence proof.
    Cross-day persistence remains a disclosed limitation.
    """
    import numpy as np
    n = math.ceil((end_ms-start_ms)/unit)
    if n < 12:
        return None
    buckets = np.zeros(n)
    for row in trades:
        j = (row['exit_ms']-start_ms)//unit
        if 0 <= j < n:
            buckets[j] += row[field]
    rng = np.random.default_rng(seed+unit//DAY)
    # Limit memory for daily 2-year blocks while keeping fixed sampling.
    draws = [buckets[rng.integers(0,n,size=(100,n))].sum(axis=1)
             for _ in range(BOOTSTRAP_REPETITIONS//100)]
    return float(np.quantile(np.concatenate(draws),alpha))


def summarize(trades: list[dict], start_ms: int, end_ms: int) -> dict:
    net = sum(r['net_usdt'] for r in trades)
    wins = sum(r['net_usdt'] for r in trades if r['net_usdt'] > 0)
    losses = -sum(r['net_usdt'] for r in trades if r['net_usdt'] < 0)
    return {'trades': len(trades), 'censored_period_end_marks': sum(r['censored'] for r in trades),
            'net_usdt': net, 'gross_usdt': sum(r['gross_usdt'] for r in trades),
            'fees_usdt': sum(r['fees_usdt'] for r in trades),
            'funding_usdt': sum(r['funding_usdt'] for r in trades),
            'profit_factor': wins/losses if losses else None,
            'day_resample_5pct_total_usdt': grouped_lower(trades,start_ms,end_ms,DAY,'net_usdt'),
            'week_resample_5pct_total_usdt': grouped_lower(trades,start_ms,end_ms,7*DAY,'net_usdt'),
            'maximum_margin_usdt': max((r['margin_usdt'] for r in trades),default=0),
            'first_entry_utc': utc(min(r['entry_ms'] for r in trades)) if trades else None,
            'last_exit_utc': utc(max(r['exit_ms'] for r in trades)) if trades else None}


def calibrate(markets: dict, opportunities: dict, spec_sha256: str) -> dict:
    cells = {}
    base, stress = [], []
    for symbol,pairs in opportunities.items():
        for signal,path in pairs:
            if path['censored']:
                continue
            for mult,output in ((1,base),(2,stress)):
                row=materialize(markets[symbol],signal,path,5,mult,True,ACCOUNT_SNAPSHOT,True)
                if row is not None:
                    output.append(row)
    for symbol in sorted(markets):
        for side in ('long','short'):
            for bucket in ('confirmed_volume','high_volume'):
                key=f'{symbol}|{side}|{bucket}'
                rows=[r for r in base if r['state']==key]
                stressed=[r for r in stress if r['state']==key]
                n=len(rows)
                successes=sum(r['net_bps']>0 for r in rows)
                probability=successes/n if n else None
                z=1.6448536269514722
                lower=(probability+z*z/(2*n)-z*math.sqrt(probability*(1-probability)/n+z*z/(4*n*n)))/(1+z*z/n) if n else None
                active_weeks=len({(r['exit_ms']-TRAIN_START)//(7*DAY) for r in rows})
                blocks=[]
                for k in range(4):
                    a=TRAIN_START+(TRAIN_END-TRAIN_START)*k//4
                    b=TRAIN_START+(TRAIN_END-TRAIN_START)*(k+1)//4
                    chunk=[r for r in rows if a <= r['entry_ms'] and r['exit_ms'] < b]
                    blocks.append({'trades':len(chunk),'sum_net_bps':sum(r['net_bps'] for r in chunk)})
                day=grouped_lower(rows,TRAIN_START,TRAIN_END,DAY,'net_bps',alpha=CALIBRATION_LOWER_ALPHA)
                week=grouped_lower(rows,TRAIN_START,TRAIN_END,7*DAY,'net_bps',alpha=CALIBRATION_LOWER_ALPHA)
                day2=grouped_lower(stressed,TRAIN_START,TRAIN_END,DAY,'net_bps',alpha=CALIBRATION_LOWER_ALPHA)
                week2=grouped_lower(stressed,TRAIN_START,TRAIN_END,7*DAY,'net_bps',alpha=CALIBRATION_LOWER_ALPHA)
                failures=[]
                if n < MIN_CELL_TRADES: failures.append('INSUFFICIENT_NONOVERLAPPING_TRADES')
                if active_weeks < MIN_ACTIVE_WEEKS: failures.append('INSUFFICIENT_ACTIVE_WEEK_BLOCKS')
                if any(c['trades'] < 15 or c['sum_net_bps'] <= 0 for c in blocks): failures.append('CHRONOLOGICAL_SUBPERIOD_UNSTABLE')
                if any(x is None or x <= 0 for x in (day,week,day2,week2)): failures.append('BASE_OR_DOUBLE_COST_NET_LOWER_BOUND_NOT_POSITIVE')
                cells[key]={'trade_count':n,'active_week_blocks':active_weeks,
                            'net_win_probability':probability,'wilson_95pct_one_sided_lower':lower,
                            'average_net_bps':sum(r['net_bps'] for r in rows)/n if n else None,
                            'day_base_lower_bps':day,'week_base_lower_bps':week,
                            'day_double_cost_lower_bps':day2,'week_double_cost_lower_bps':week2,
                            'chronological_four_subperiods':blocks,
                            'statistical_gate_passed':not failures,'failure_reasons':failures}
    return {'schema_version':1,'strategy_sha256':spec_sha256,
            'training_start_utc':utc(TRAIN_START),'training_end_exclusive_utc':utc(TRAIN_END),
            'training_start_ms':TRAIN_START,'training_end_exclusive_ms':TRAIN_END,
            'source':'SHA-verified Binance USD-M 1m and funding cross-exchange proxy',
            'native_bitget_data_verified':False,'execution_and_protection_verified':False,
            'prospective_validation_verified':False,'deployment_approved':False,
            'labels':'net-positive completed nonoverlapping per-symbol trades; base .0004 fee sensitivity',
            'label_unit_notional_usdt':1.0,
            'execution_impact_assumed_linear_not_measured':True,
            'sample_warning':'Nonoverlap does not establish independence; grouped bootstrap cannot remove selection bias.',
            'gates':{'minimum_trades_per_cell':MIN_CELL_TRADES,'minimum_active_weeks':MIN_ACTIVE_WEEKS,
                     'chronological_subperiods':4,'minimum_trades_each_subperiod':15,
                     'predeclared_cell_family_count':CALIBRATION_CELL_FAMILY,
                     'familywise_lower_tail_alpha':CALIBRATION_LOWER_ALPHA,
                     'daily_and_weekly_net_familywise_lower_positive_under_cost1_and_cost2':True},
            'cells':cells,'eligible_state_count':0,'future_profitability_guaranteed':False}


def run(cache: Path, output: Path) -> dict:
    from .strategy import SPEC, spec_sha256
    markets={s:prepare(load_proxy(cache,s)) for s in ('BTCUSDT','ETHUSDT')}
    output.mkdir(parents=True,exist_ok=True)
    result={'strategy':SPEC,'strategy_sha256':spec_sha256(),'status':'RESEARCH_NO_LIVE_APPROVAL',
            'sources':{s:{**markets[s]['market'].manifest,
                          'fee_personalized':'User-provided 2026-10-01 account snapshot; sensitivity assumption, not historical fee proof'}
                       for s in markets},
            'risk':{'account_snapshot_usdt':ACCOUNT_SNAPSHOT,'snapshot_not_current_balance':True,
                    'margin_per_symbol_cap_usdt':MARGIN_CAP,'risk_fraction_of_marked_equity':RISK_FRACTION,
                    'risk_budget_cap_usdt':RISK_BUDGET_CAP,'portfolio_risk_fraction_cap':PORTFOLIO_RISK_FRACTION,
                    'reserve_usdt':RESERVE,'max_open_symbols':2,'leverage':list(LEVERAGES),
                    'maintenance_margin_and_liquidation_verified':False},
            'costs':{'public_taker_each_side':.0006,'observed_personal_sensitivity_each_side':.0004,
                     'full_spread_assumed_bps':FULL_SPREAD_BPS,'impact_each_side_assumed_bps':SIDE_IMPACT_BPS,
                     'base_roundtrip_bps_personal':25,'base_roundtrip_bps_public':29,
                     'multipliers':[1,2],'funding':'actual Binance proxy rates; adverse cash doubled at cost2',
                     'Bitget_historical_quote_fill_evidence':False},
            'validation':{'untouched_out_of_sample':False,'2025_onward_previously_viewed':True,
                          'both_historical_periods_already_viewed':True,
                          'rule_search_performed':False,'exchange_write_requests':0,
                          'initial_position_state':'flat separately at each research period start',
                          'daily_loss_halt_simulated':False,
                          'runtime_equivalence_verified':False,
                          'portfolio_opportunity_universe':'predeclared per-symbol nonoverlapping paths; rejected entries do not reopen overlapping candidates',
                          'portfolio_rejected_entry_path_thinning_not_guaranteed_pnl_conservative':True,
                          'runtime_daily_halt_partial_fill_and_native_guard_protocol_not_replayed':True,
                          'intrabar_ordering':'old native guard first, then completed MTF decisions after 60 seconds; adverse opening gap; event open funding mark'},
            'ranges':{}}
    training_opportunities=None
    for name,start,end in (('train_2023_2024',TRAIN_START,TRAIN_END),
                           ('previously_viewed_2025_2026',TRAIN_END,ARCHIVE_END)):
        signals={s:candidates(m,start,end) for s,m in markets.items()}
        opportunities={s:independent_opportunities(markets[s],rows,end) for s,rows in signals.items()}
        if start==TRAIN_START: training_opportunities=opportunities
        block={'candidate_events_by_symbol':{s:len(rows) for s,rows in signals.items()},
               'nonoverlapping_paths_by_symbol':{s:len(rows) for s,rows in opportunities.items()},
               'scenarios':{}}
        for lev in LEVERAGES:
            for fee in ('public','personal'):
                for mult in (1,2):
                    key=f'L{lev}_{fee}_cost{mult}'
                    trades,rejected=run_portfolio(markets,opportunities,lev,mult,fee=='personal')
                    block['scenarios'][key]={'portfolio':summarize(trades,start,end),
                        'by_symbol':{s:summarize([r for r in trades if r['symbol']==s],start,end) for s in markets},
                        'rejected':rejected}
                    with (output/f'{name}_{key}_trades.csv').open('w',newline='') as f:
                        if trades:
                            writer=csv.DictWriter(f,fieldnames=list(trades[0]));writer.writeheader();writer.writerows(trades)
                        else: f.write('no_trades\n')
        result['ranges'][name]=block
    model=calibrate(markets,training_opportunities,spec_sha256())
    serialized=json.dumps(model,indent=2,allow_nan=False)+'\n'
    (output/'probability_model.json').write_text(serialized)
    result['model_sha256']=hashlib.sha256(serialized.encode()).hexdigest()
    result['statistical_gate_passed_states']=[k for k,c in model['cells'].items() if c['statistical_gate_passed']]
    result['eligible_live_states']=0
    (output/'summary.json').write_text(json.dumps(result,indent=2,allow_nan=False)+'\n')
    return result


def main() -> None:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cache',type=Path,
                        default=Path(__file__).resolve().parents[4]/'btc_eth_15m_backtest'/'cache')
    parser.add_argument('--output',type=Path,default=Path(__file__).resolve().parent/'results')
    args=parser.parse_args()
    result=run(args.cache,args.output)
    print(json.dumps({'strategy_sha256':result['strategy_sha256'],
                      'eligible_live_states':result['eligible_live_states'],
                      'statistical_gate_passed_states':result['statistical_gate_passed_states'],
                      'scenarios':{name:block['scenarios']['L5_personal_cost1']['portfolio']
                                   for name,block in result['ranges'].items()}},allow_nan=False))


if __name__ == '__main__':
    main()
