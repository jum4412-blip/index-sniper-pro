"""Frozen supplementary-filter experiment. No API or exchange transport.

All indicators see completed bars. Each filtered portfolio selects anew from
the FULL candidate pool; rejected/occupied candidates do not create labels.
Historical proxy execution and calendar resampling are diagnostics, not live
execution proof or an untouched out-of-sample result.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import csv
import gzip
import hashlib
import json
import math
from pathlib import Path

import numpy as np
from ._replay import research as replay
from ._replay.strategy import spec_sha256

PERIODS = (
    ('train_2023_2024', replay.TRAIN_START, replay.TRAIN_END),
    ('previously_viewed_2025_2026', replay.TRAIN_END, replay.ARCHIVE_END),
)
BOOT_REPS = 20000
BOOT_SEED = 73217
MIN_TRADES = 100
MIN_WEEKS = 20
MIN_SUBPERIOD_TRADES = 15
ALPHA = .05 / (40 * 2)


def canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)+'\n')


def frozen_protocol(specs):
    if len(specs) != 40 or len({s['id'] for s in specs}) != 40:
        raise ValueError('Exactly40 predeclaredfilters required')
    return {
        'experiment': 'MTF_TURTLE_SUPPLEMENTARY_SINGLE_ENTRY_FILTERS_V1',
        'created_utc': datetime.now(timezone.utc).isoformat(),
        'baseline_strategy_sha256': spec_sha256(),
        'indicators_sha256': sha(Path(__file__).with_name('indicators.py')),
        'runner_sha256': sha(__file__),
        'replay_sha256': sha(Path(replay.__file__)),
        'baseline': 'W_D_H4_H1_M5 structure adaptation; not OriginalTurtle20_55day system',
        'baseline_exits_and_risk_sizing_unchanged': True,
        'filters': specs,
        'parameter_search': False, 'combination_search': False,
        'selection': 'sample-sufficient first, then training personal cost2 net USDT, then training personal cost1 net USDT, then id',
        'diagnostic_period_used_to_choose_winner': False,
        'minimum_completed_trades_each_coin': MIN_TRADES,
        'minimum_active_weeks_each_coin': MIN_WEEKS,
        'minimum_trades_per_four_chronological_blocks_each_coin': MIN_SUBPERIOD_TRADES,
        'familywise_alpha': ALPHA, 'family_count': 80,
        'bootstrap_repetitions': BOOT_REPS, 'bootstrap_seed': BOOT_SEED,
        'gate': 'both coins: four subperiods positive, day/week net and paired baseline improvement lower bounds positive under personal base and doubled costs; train plus diagnostic repeat',
        'periods': [dict(id=p,start_ms=s,end_exclusive_ms=e) for p,s,e in PERIODS],
        'scenario_count': 41*2*2*2,
        'fees': {'personal_each_side': .0004, 'public_each_side': .0006},
        'spread_full_bps': 3, 'impact_each_side_bps': 7,
        'cost_multipliers': [1,2], 'leverage': 5,
        'margin_cap_per_coin_usdt': 300, 'maximum_positions': 2,
        'initial_reference_equity_each_period_usdt': replay.ACCOUNT_SNAPSHOT,
        'minimum_reserve_usdt': replay.RESERVE,
        'source_hashes': replay.PROXY_HASHES,
        'untouched_out_of_sample': False,
        'native_bitget_execution_verified': False,
        'deployment_approved': False,
        'frozen_before_indicator_results': True,
    }


def build_pools(markets, work):
    """Precompute exits independent of ANY extra indicator or filter result."""
    work.mkdir(parents=True,exist_ok=True)
    pools={}
    for period,start,end in PERIODS:
        pools[period]={}
        for symbol,prepared in markets.items():
            filename=work/(period+'_'+symbol+'_all_candidates.json.gz')
            identity={'strategy_sha256':spec_sha256(),'minute_sha256':prepared['market'].manifest['minute_sha256'],
                      'replay_sha256':sha(replay.__file__),'start_ms':start,'end_ms':end}
            if filename.exists():
                with gzip.open(filename,'rt') as f: cached=json.load(f)
                if cached['identity']!=identity:raise ValueError('Candidatecacheidentitychanged')
                pairs=cached['pairs']
            else:
                signals=replay.candidates(prepared,start,end)
                print('candidate_pool',period,symbol,len(signals),flush=True)
                pairs=[]
                for n,signal in enumerate(signals):
                    path=replay.replay_path(prepared,signal,end)
                    lean={k:signal[k] for k in ('symbol','side','guard','score','event_id','m5_j','entry_i','entry_ms','entry_ref')}
                    lean['metadata']={'volume_ratio':signal['metadata']['volume_ratio']}
                    pairs.append([lean,path])
                    if (n+1)%1000==0: print('paths',period,symbol,n+1,flush=True)
                with gzip.open(filename,'wt') as f:json.dump({'identity':identity,'pairs':pairs},f,allow_nan=False)
            pools[period][symbol]=pairs
            print('pool_ready',period,symbol,len(pairs),flush=True)
    return pools


def decision_mask(series, width_ms, signals, long_mask, short_mask, valid):
    """Exact last COMPLETE indicator bar as of baseline signal M5 close."""
    if any(len(x)!=len(series) for x in (long_mask,short_mask,valid)):
        raise ValueError('Filterarrayalignmentinvalid')
    if not len(series):
        return np.zeros(len(signals),dtype=bool),np.zeros(len(signals),dtype=bool)
    closes=series[:,0]+width_ms
    times=np.array([s['entry_ms']-replay.MIN for s in signals],dtype=np.int64)
    idx=np.searchsorted(closes,times,side='right')-1
    inrange=idx>=0;safe=np.maximum(idx,0)
    sides=np.array([s['side']=='long' for s in signals])
    result=inrange & valid[safe] & np.where(sides,long_mask[safe],short_mask[safe])
    return result, inrange & valid[safe]


def selected_pairs(prepared, pairs, spec, computed):
    if spec is None:return pairs, np.ones(len(pairs),dtype=bool),len(pairs)
    values=computed[spec['id']];series=prepared['frames'][spec['frame']]
    mask,valid=decision_mask(series,replay.FRAME_WIDTHS[spec['frame']],
                             [p[0] for p in pairs],values['long'],values['short'],values['valid'])
    return [p for p,allowed in zip(pairs,mask) if allowed],mask,int(valid.sum())


def portfolio(markets, pools, multiplier, personal=True):
    """Replay acceptance from all events, with causal equity at each entry.

    Cached future exits schedule already-open trades but their future PnL is
    never used to size another entry. Only settled PnL and contemporary minute
    opens plus incurred entry fees/funding contribute to marked equity.
    """
    active=[];trades=[];rejected=Counter();realized=replay.ACCOUNT_SNAPSHOT
    merged=sorted([pair for pairs in pools.values() for pair in pairs],
                  key=lambda p:(p[0]['entry_ms'],-p[0]['score'],p[0]['symbol']))
    for signal,path in merged:
        now=signal['entry_ms']
        realized+=sum(row['net_usdt'] for row in active if row['exit_ms']<now)
        active=[row for row in active if row['exit_ms']>=now]
        if len(active)>=2 or any(row['symbol']==signal['symbol'] for row in active):
            rejected['position_limit']+=1;continue
        equity=realized
        for row in active:
            market=markets[row['symbol']]['market'];sign=1 if row['side']=='long' else -1
            i=(now-int(market.minute[0,0]))//replay.MIN
            equity+=sign*(float(market.minute[i,1])-row['entry'])*row['qty']-row['entry_fees_usdt']
            equity+=replay.funding_cash(market,row['entry_ms'],{'exit_ms':now,'intraminute':False},sign,row['qty'],multiplier)
        row=replay.materialize(markets[signal['symbol']],signal,path,5,multiplier,personal,equity)
        if row is None:rejected['size_or_risk']+=1;continue
        if sum(r['planned_guard_risk_usdt'] for r in active)+row['planned_guard_risk_usdt']>equity*replay.PORTFOLIO_RISK_FRACTION+1e-9:
            rejected['aggregate_risk']+=1;continue
        if equity-sum(r['margin_usdt'] for r in active)<row['margin_usdt']+replay.RESERVE+1e-9:
            rejected['cash_reserve']+=1;continue
        row['exit_intraminute']=path['intraminute']
        trades.append(row);active.append(row)
    return sorted(trades,key=lambda r:(r['exit_ms'],r['symbol'])),dict(rejected)


def drawdown_proxy(markets, trades, start, end):
    """Marked equity at 1-minute opens, not worst intraminute/Bitget margin."""
    n=(end-start)//replay.MIN
    if n<=0:raise ValueError('Emptyperiod')
    changes=np.zeros(n+1);mark=np.zeros(n+1)
    for row in trades:
        market=markets[row['symbol']]['market'];sign=1 if row['side']=='long' else -1
        a=max(0,(row['entry_ms']-start)//replay.MIN)
        release=min(n,(row['exit_ms']-start)//replay.MIN+1)
        offset=(start-int(market.minute[0,0]))//replay.MIN
        prices=market.minute[offset+a:offset+release,1]
        mark[a:release]+=sign*(prices-row['entry'])*row['qty']
        changes[a]-=row['entry_fees_usdt']
        events=market.funding
        lower=np.searchsorted(events[:,0],row['entry_ms'],'left')
        boundary=row['exit_ms']+(replay.MIN if row['exit_intraminute'] else 0)
        upper=np.searchsorted(events[:,0],boundary,'left' if row['exit_intraminute'] else 'right')
        used=events[lower:upper]
        indices=np.searchsorted(market.minute[:,0],used[:,0],'right')-1
        cash=-sign*row['qty']*used[:,1]*market.minute[indices,1]
        ambiguous=(used[:,0]==row['entry_ms'])|(used[:,0]>=row['exit_ms'])
        cash[ambiguous]=np.minimum(cash[ambiguous],0)
        cash=np.where(cash<0,cash*row['cost_multiplier'],cash)
        if not math.isclose(float(cash.sum()),row['funding_usdt'],abs_tol=1e-7):raise ValueError('Fundingcurvemismatch')
        idx=((used[:,0]-start)//replay.MIN).astype(int)
        for i,value in zip(idx,cash):
            if 0<=i<=n:changes[i]+=value
        changes[release]+=row['net_usdt']+row['entry_fees_usdt']-row['funding_usdt']
    curve=replay.ACCOUNT_SNAPSHOT+np.cumsum(changes)+mark
    terminal=replay.ACCOUNT_SNAPSHOT+sum(r['net_usdt'] for r in trades)
    if not math.isclose(float(curve[-1]),terminal,abs_tol=1e-6):raise ValueError('Equitycurvemismatch')
    peaks=np.maximum.accumulate(np.r_[replay.ACCOUNT_SNAPSHOT,curve])[1:]
    dd=(peaks-curve)/peaks
    return float(dd.max()),float(curve.min())


def metrics(rows, start, end, dd=None):
    complete=[r for r in rows if not r['censored']]
    win=sum(r['net_usdt'] for r in complete if r['net_usdt']>0)
    loss=-sum(r['net_usdt'] for r in complete if r['net_usdt']<0)
    blocks=[]
    for k in range(4):
        a=start+(end-start)*k//4;b=start+(end-start)*(k+1)//4
        chunk=[r for r in complete if a<=r['entry_ms']<b]
        blocks.append({'completed_trades':len(chunk),'net_usdt':sum(r['net_usdt'] for r in chunk)})
    return {'completed_trades':len(complete),'censored_period_end_marks':len(rows)-len(complete),
            'total_marked_net_usdt':sum(r['net_usdt'] for r in rows),
            'completed_net_usdt':sum(r['net_usdt'] for r in complete),
            'net_win_fraction':sum(r['net_usdt']>0 for r in complete)/len(complete) if complete else None,
            'profit_factor_completed':win/loss if loss else None,
            'fees_usdt':sum(r['fees_usdt'] for r in rows),'funding_usdt':sum(r['funding_usdt'] for r in rows),
            'entry_notional_turnover_usdt':sum(r['notional_usdt'] for r in rows),
            'active_week_blocks':len({(r['exit_ms']-replay.WEEK_ORIGIN)//replay.WEEK for r in complete}),
            'chronological_four_blocks':blocks,
            'sample_sufficient':len(complete)>=MIN_TRADES and len({(r['exit_ms']-replay.WEEK_ORIGIN)//replay.WEEK for r in complete})>=MIN_WEEKS and all(b['completed_trades']>=MIN_SUBPERIOD_TRADES for b in blocks),
            'maximum_drawdown_1m_open_proxy':dd[0] if dd else None,
            'minimum_equity_1m_open_proxy_usdt':dd[1] if dd else None}


def calendar_buckets(rows, start, end, unit):
    origin=replay.WEEK_ORIGIN if unit==replay.WEEK else 0
    first=origin+((start-origin)//unit)*unit
    counts=math.ceil((end-first)/unit);out=np.zeros(counts)
    for row in rows:
        if row['censored'] or not start<=row['exit_ms']<end:continue
        i=(row['exit_ms']-first)//unit
        if 0<=i<counts:out[i]+=row['net_usdt']
    return out


def calendar_bootstrap(all_trades, start, end, unit):
    """Same draws for every strategy/coin/cost preserve paired comparisons.

    Iid day/week block resampling is a finite exploratory diagnostic and does
    not eliminate cross-block dependence, model search or proxy execution error.
    """
    labels=[];series=[]
    for key,rows in all_trades.items():
        for coin in ('BTCUSDT','ETHUSDT','PORTFOLIO'):
            selected=rows if coin=='PORTFOLIO' else [r for r in rows if r['symbol']==coin]
            labels.append((key,coin));series.append(calendar_buckets(selected,start,end,unit))
    data=np.array(series);n=data.shape[1]
    draws=np.empty((BOOT_REPS,len(labels)))
    rng=np.random.default_rng(BOOT_SEED+unit//replay.DAY+start//replay.DAY)
    probabilities=np.full(n,1/n)
    for a in range(0,BOOT_REPS,1000):
        size=min(1000,BOOT_REPS-a)
        weights=rng.multinomial(n,probabilities,size=size).astype(float)
        draws[a:a+size]=weights@data.T
    positions={label:i for i,label in enumerate(labels)}
    out={}
    for i,(key,coin) in enumerate(labels):
        variant,fee,cost=key.split('|')
        base=positions[(f'baseline|{fee}|{cost}',coin)]
        delta=draws[:,i]-draws[:,base]
        out[(key,coin)]={'net_lower_5pct_usdt':float(np.quantile(draws[:,i],.05)),
                         'net_lower_familywise_usdt':float(np.quantile(draws[:,i],ALPHA)),
                         'incremental_lower_5pct_usdt':float(np.quantile(delta,.05)),
                         'incremental_lower_familywise_usdt':float(np.quantile(delta,ALPHA))}
    return out


def variant_gate(block, variant):
    reasons=[]
    for cost in (1,2):
        result=block[f'{variant}|personal|cost{cost}']
        for coin,stat in result['by_symbol'].items():
            if not stat['sample_sufficient']:reasons.append(f'{coin}:cost{cost}:SAMPLE_INSUFFICIENT')
            if not all(b['net_usdt']>0 for b in stat['chronological_four_blocks']):reasons.append(f'{coin}:cost{cost}:CHRONOLOGICAL_INSTABILITY')
            for unit in ('day','week'):
                bounds=stat['bootstrap'][unit]
                if bounds['net_lower_familywise_usdt']<=0:reasons.append(f'{coin}:cost{cost}:{unit}:NET_LOWER_NOT_POSITIVE')
                if bounds['incremental_lower_familywise_usdt']<=0:reasons.append(f'{coin}:cost{cost}:{unit}:IMPROVEMENT_LOWER_NOT_POSITIVE')
    return {'passed':not reasons,'failure_reasons':reasons}


def run(cache,output,work,build_only=False):
    output.mkdir(parents=True,exist_ok=True)
    protocol=None
    if not build_only:
        from .indicators import FILTER_SPECS
        protocol=frozen_protocol(FILTER_SPECS)
        destination=output/'frozen_protocol.json'
        if destination.exists():raise ValueError('Use aNEWoutputdirectory; frozenresultsnot overwritten')
        write_json(destination,protocol)
        protocol_digest=sha(destination)
        print('FROZEN',protocol_digest,flush=True)
    markets={s:replay.prepare(replay.load_proxy(cache,s)) for s in ('BTCUSDT','ETHUSDT')}
    pools=build_pools(markets,work)
    if build_only:return {'pools_ready':True}
    from .indicators import compute_filters
    features={s:compute_filters(m['frames']) for s,m in markets.items()}
    print('features_ready',len(FILTER_SPECS),flush=True)
    variants=[None]+list(FILTER_SPECS)
    summary={'protocol_sha256':protocol_digest,'source':'SHA-verified Binance USD-M1m+funding cross-exchange proxy',
             'status':'EXPLORATORY_RESEARCH_NO_LIVE_APPROVAL','periods':{},'exchange_orders_sent':0,
             'risk_execution_verified':False,'native_bitget_data_verified':False,'future_profitability_guaranteed':False,
             'limitations':['historicalperiodspreviouslyviewed; no untouchedholdout',
                            'singlethresholdperspecifiedindicator; notallpossibleindicatorsorparameters',
                            'originalTurtleandATRsizingnotreproduced',
                            'dailyweeklylosshaltsandruntimeMDDentryhaltsnotreplayed',
                            'MM/liquidation/partialfills/nativeprotection/notionalimpactnotmeasured',
                            'drawdownusesminuteopens; intraminuteworstcasecanbeworse',
                            'day/weekiidgroupedbootstrapcannotproveindependenceorremovesearchbias',
                            'actualinstitutional/orderbook/openinterest/liquidationflowunavailable']}
    for period,start,end in PERIODS:
        all_trades={};block={};accepted_masks={}
        for spec in variants:
            name=spec['id'] if spec else 'baseline';filtered={};candidate_counts={}
            for coin,prepared in markets.items():
                selected,mask,valid_count=selected_pairs(prepared,pools[period][coin],spec,features[coin])
                filtered[coin]=selected;accepted_masks[(name,coin)]=mask
                candidate_counts[coin]={'baseline_events':len(mask),'indicator_valid_events':valid_count,
                                        'indicator_pass_events':int(mask.sum())}
            for fee,personal in (('personal',True),('public',False)):
                for cost in (1,2):
                    key=f'{name}|{fee}|cost{cost}'
                    trades,rejected=portfolio(markets,filtered,cost,personal)
                    all_trades[key]=trades
                    dd=drawdown_proxy(markets,trades,start,end)
                    block[key]={'portfolio':metrics(trades,start,end,dd),
                                'by_symbol':{coin:metrics([r for r in trades if r['symbol']==coin],start,end) for coin in markets},
                                'candidate_counts':candidate_counts,'rejected':rejected}
                    with (output/f'{period}__{name}__{fee}__cost{cost}_trades.csv').open('w',newline='') as f:
                        if trades:
                            writer=csv.DictWriter(f,fieldnames=list(trades[0]));writer.writeheader();writer.writerows(trades)
                        else:f.write('no_trades\n')
            print('portfolio_ready',period,name,flush=True)
        for unit_name,unit in (('day',replay.DAY),('week',replay.WEEK)):
            bootstrap=calendar_bootstrap(all_trades,start,end,unit)
            for key,result in block.items():
                result['portfolio'].setdefault('bootstrap',{})[unit_name]=bootstrap[(key,'PORTFOLIO')]
                for coin,stat in result['by_symbol'].items():stat.setdefault('bootstrap',{})[unit_name]=bootstrap[(key,coin)]
            print('bootstrap_ready',period,unit_name,flush=True)
        gates={spec['id']:variant_gate(block,spec['id']) for spec in FILTER_SPECS}
        overlaps=[]
        for i,spec in enumerate(FILTER_SPECS):
            for other in FILTER_SPECS[i+1:]:
                intersection=union=0
                for coin in markets:
                    a=accepted_masks[(spec['id'],coin)];b=accepted_masks[(other['id'],coin)]
                    intersection+=int((a&b).sum());union+=int((a|b).sum())
                overlaps.append({'filter_a':spec['id'],'filter_b':other['id'],
                                 'candidate_jaccard':intersection/union if union else None})
        summary['periods'][period]={'scenarios':block,'gates':gates,'filter_overlap':overlaps}
        write_json(output/f'{period}_summary.json',summary['periods'][period])
    train=summary['periods'][PERIODS[0][0]];diagnostic=summary['periods'][PERIODS[1][0]]
    def rank(spec):
        one=train['scenarios'][spec['id']+'|personal|cost1'];two=train['scenarios'][spec['id']+'|personal|cost2']
        adequate=all(stat['sample_sufficient'] for stat in one['by_symbol'].values()) and all(stat['sample_sufficient'] for stat in two['by_symbol'].values())
        return (-int(adequate),-two['portfolio']['total_marked_net_usdt'],-one['portfolio']['total_marked_net_usdt'],spec['id'])
    ranked=sorted(FILTER_SPECS,key=rank)
    summary['training_only_ranking']=[spec['id'] for spec in ranked]
    summary['training_selected_filter']=ranked[0]['id']
    summary['statistical_pass_train']=[name for name,g in train['gates'].items() if g['passed']]
    summary['statistical_pass_diagnostic_repeat']=[name for name,g in diagnostic['gates'].items() if g['passed']]
    summary['exploratory_joint_pass']=[name for name in summary['statistical_pass_train'] if diagnostic['gates'][name]['passed']]
    summary['eligible_live_states']=0
    write_json(output/'summary.json',summary)
    rows=[]
    for period,block in summary['periods'].items():
        for key,result in block['scenarios'].items():
            variant,fee,cost=key.split('|')
            for coin,stat in [('PORTFOLIO',result['portfolio'])]+list(result['by_symbol'].items()):
                record={'period':period,'filter':variant,'fee':fee,'cost':cost,'scope':coin}
                record.update({k:v for k,v in stat.items() if not isinstance(v,(list,dict))})
                for unit,bounds in stat['bootstrap'].items():record.update({unit+'_'+k:v for k,v in bounds.items()})
                record['gate_pass']=block['gates'].get(variant,{}).get('passed',False)
                rows.append(record)
    with (output/'comparison_all.csv').open('w',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
    write_json(output/'filter_definitions.json',FILTER_SPECS)
    print(json.dumps({'training_selected_filter':summary['training_selected_filter'],
                      'training_pass':summary['statistical_pass_train'],'joint_pass':summary['exploratory_joint_pass'],
                      'live_pass':0},ensure_ascii=False),flush=True)
    return summary


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cache',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--work',type=Path,required=True)
    parser.add_argument('--build-only',action='store_true')
    args=parser.parse_args()
    run(args.cache,args.output,args.work,args.build_only)


if __name__=='__main__':main()
