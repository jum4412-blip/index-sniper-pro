"""Frozen, read-only CCI/CHOP placement comparison from complete candle windows.

No exchange transport exists in this project. Historical sources are previously
viewed Binance USD-M proxies and cannot prove a Bitget execution advantage.
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import json
from pathlib import Path
import numpy as np

from . import runner_base as base
from ._replay import research as replay
from ._replay.strategy import spec_sha256

FRAMES = ('W', 'D', 'H12', 'H6', 'H4', 'H1', 'M30', 'M15', 'M5', 'M3', 'M1')
WIDTHS = dict(W=replay.WEEK, D=replay.DAY, H12=12*replay.HOUR,
              H6=6*replay.HOUR, H4=replay.H4, H1=replay.HOUR,
              M30=30*replay.MIN, M15=15*replay.MIN, M5=replay.M5,
              M3=3*replay.MIN, M1=replay.MIN)
BOOT_REPS = 100000
ALPHA = .05/(144*2)


def variants():
    return ([dict(id='baseline', kind='baseline', cci_frame=None, chop_frame=None)]
            + [dict(id='cci_'+f.lower(), kind='cci', cci_frame=f, chop_frame=None) for f in FRAMES]
            + [dict(id='chop_'+f.lower(), kind='chop', cci_frame=None, chop_frame=f) for f in FRAMES]
            + [dict(id='cci_'+a.lower()+'_chop_'+b.lower(), kind='combined', cci_frame=a, chop_frame=b)
               for a in FRAMES for b in FRAMES])


def protocol():
    paths = [Path(__file__), Path(base.__file__), Path(replay.__file__),
             Path(__file__).parent/'_replay'/'strategy.py']
    return dict(
        experiment='MTF_CCI_CHOP_ALL_SUPPORTED_PLACEMENTS_V1',
        created_utc=datetime.now(timezone.utc).isoformat(),
        baseline_strategy_sha256=spec_sha256(),
        source_code_sha256={str(p.relative_to(Path(__file__).parent)):base.sha(p) for p in paths},
        source_hashes=replay.PROXY_HASHES,
        frames=list(FRAMES), variants=variants(), variant_count=144,
        cci={'period':20, 'long_min':100, 'short_max':-100, 'price':'(high+low+close)/3',
             'mean_deviation':'mean absolute deviation from same 20-window mean'},
        chop={'period':14, 'max':38.2, 'warmup_bars':15,
              'formula':'100*log10(sum(true_range14)/(max(high14)-min(low14)))/log10(14)'},
        exact_latest_completed_window_required=True,
        forming_candles_excluded=True, gap_or_stale_window_denies=True,
        weekly_origin='Monday 00:00 UTC', decision_time='baseline entry_ms minus 60000ms',
        entry_only=True, base_structure_exits_unchanged=True,
        cci_chop_exit_parameter_optimization=False,
        periods=[dict(id=n,start_ms=s,end_exclusive_ms=e) for n,s,e in base.PERIODS],
        scenario_count=1152, fees_each_side={'personal':.0004,'public':.0006},
        full_spread_bps=3, impact_each_side_bps=7, cost_multipliers=[1,2],
        leverage=5, margin_cap_each_coin_usdt=300, maximum_simultaneous_positions=2,
        initial_reference_equity_usdt=replay.ACCOUNT_SNAPSHOT, reserve_usdt=replay.RESERVE,
        selection='combined121 only; sample-sufficient both coins under personal costs1/2 first; then TRAIN personal doubled-cost net; then TRAIN personal base-cost net; then id',
        diagnostic_period_used_to_choose=False,
        minimum_completed_trades_each_coin=100, minimum_active_weeks_each_coin=20,
        minimum_trades_each_four_chronological_blocks=15,
        multiple_comparison_family=288, corrected_lower_quantile=ALPHA,
        bootstrap_repetitions=BOOT_REPS, bootstrap_seed=base.BOOT_SEED,
        bootstrap='calendar day/week multinomial resampling; coins and baseline use same draws; zero blocks retained',
        bootstrap_short_circuit='necessary sample and positive-four-block conditions fail => reject without bootstrap; selected and baseline always report bounds',
        untouched_out_of_sample=False, data='SHA-verified Binance USD-M 1m plus funding proxy',
        live_execution_verified=False, deployment_approved=False,
        frozen_before_new_placement_results=True,
    )


def window_values(series, frame, signals):
    """Compute only the candidate-time windows, not an unnecessary full array."""
    width=WIDTHS[frame];origin=replay.WEEK_ORIGIN if frame=='W' else 0
    decisions=np.asarray([s['entry_ms']-replay.MIN for s in signals],dtype=np.int64)
    count=len(decisions)
    cci=np.full(count,np.nan);chop=np.full(count,np.nan)
    if not len(series):return {'cci':cci,'chop':chop,'cci_valid':np.zeros(count,bool),'chop_valid':np.zeros(count,bool)}
    idx=np.searchsorted(series[:,0]+width,decisions,side='right')-1
    safe=np.maximum(idx,0)
    expected=origin+((decisions-origin)//width)*width-width
    latest=(idx>=0)&(series[safe,0]==expected)
    for size,name in ((20,'cci'),(15,'chop')):
        valid=latest & (idx>=size-1)
        rows=np.flatnonzero(valid)
        if not len(rows):continue
        offsets=idx[rows,None]+np.arange(1-size,1)
        windows=series[offsets]
        continuous=(np.diff(windows[:,:,0],axis=1)==width).all(axis=1)
        finite=np.isfinite(windows).all(axis=(1,2))
        validrows=continuous&finite
        rows=rows[validrows];windows=windows[validrows]
        if name=='cci':
            typical=(windows[:,:,2]+windows[:,:,3]+windows[:,:,4])/3
            mean=typical.mean(axis=1);mad=np.abs(typical-mean[:,None]).mean(axis=1)
            good=mad>0
            cci[rows[good]]=(typical[good,-1]-mean[good])/(.015*mad[good])
        else:
            high=windows[:,1:,2];low=windows[:,1:,3];prev=windows[:,:-1,4]
            true_range=np.maximum(high-low,np.maximum(np.abs(high-prev),np.abs(low-prev)))
            span=high.max(axis=1)-low.min(axis=1);total=true_range.sum(axis=1)
            good=(span>0)&(total>0)
            chop[rows[good]]=100*np.log10(total[good]/span[good])/np.log10(14)
    return {'cci':cci,'chop':chop,'cci_valid':np.isfinite(cci),'chop_valid':np.isfinite(chop)}


def mask_for(spec, features, pairs):
    sides=np.asarray([p[0]['side']=='long' for p in pairs])
    mask=np.ones(len(pairs),bool);valid=mask.copy()
    if spec['cci_frame']:
        values=features[spec['cci_frame']];valid &= values['cci_valid']
        mask &= values['cci_valid'] & np.where(sides,values['cci']>=100,values['cci']<=-100)
    if spec['chop_frame']:
        values=features[spec['chop_frame']];valid &= values['chop_valid']
        mask &= values['chop_valid'] & (values['chop']<=38.2)
    return mask,valid


def necessary_gate(block,name):
    reasons=[]
    for cost in (1,2):
        for coin,stat in block[f'{name}|personal|cost{cost}']['by_symbol'].items():
            if not stat['sample_sufficient']:reasons.append(f'{coin}:cost{cost}:SAMPLE_INSUFFICIENT')
            if not all(b['net_usdt']>0 for b in stat['chronological_four_blocks']):
                reasons.append(f'{coin}:cost{cost}:CHRONOLOGICAL_INSTABILITY')
    return reasons


def choose(block, definitions):
    combined=[s for s in definitions if s['kind']=='combined']
    def rank(spec):
        one=block[spec['id']+'|personal|cost1'];two=block[spec['id']+'|personal|cost2']
        sufficient=all(x['sample_sufficient'] for result in (one,two) for x in result['by_symbol'].values())
        return (-int(sufficient),-two['portfolio']['total_marked_net_usdt'],
                -one['portfolio']['total_marked_net_usdt'],spec['id'])
    ranked=sorted(combined,key=rank)
    return ranked[0],[s['id'] for s in ranked]


def write_trades(path,rows):
    with path.open('w',newline='') as f:
        if not rows:f.write('no_trades\n');return
        writer=csv.DictWriter(f,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)


def report(summary,output):
    chosen=summary['selected_rule'];name=chosen['id']
    lines=['CCI·CHOP 전체 시간봉 배치 비교', '',
           '주봉·일봉·12/6/4/1시간·30/15/5/3/1분: 11개 시간봉을 비교했다.',
           '기준 1개 + CCI 단독 11개 + CHOP 단독 11개 + 두 지표 독립 배치 121개 = 144개 변형.',
           '수수료 2종·비용 1/2배·기간 2개를 적용한 1,152개 포트폴리오 시나리오.',
           '2023–2024 학습 결과로만 선정했다. 2025–2026.09는 이미 열람한 진단 기간이다.',
           'Binance 원천자료의 교차거래소 대용시험이며 Bitget 실체결 검증이 아니다.', '',
           f"학습 기준 선정 조합: CCI20={chosen['cci_frame']}, CHOP14={chosen['chop_frame']}",
           'CCI 롱 ≥100 / 숏 ≤−100, CHOP ≤38.2. 파라미터 최적화 없이 배치만 대조.',
           '주·일 추세 → 4시간/1시간 구조 → 5분 거래량 돌파가 기본 진입 조건이다.',
           '두 지표는 신규 진입 필터이며, 구조 기반 보호가·추세 무효화 청산은 같은 규칙을 유지했다.',
           '선정 순서: 두 코인 표본 기준 충족 → 학습 2배 비용 순익 → 학습 기본 비용 순익 → 이름.', '',
           '기간 | 비용 | BTC 거래/순익USDT | ETH 거래/순익USDT | 포트폴리오 순익USDT']
    for period,block in summary['periods'].items():
        for cost in (1,2):
            result=block['scenarios'][f'{name}|personal|cost{cost}'];a=result['by_symbol']['BTCUSDT'];b=result['by_symbol']['ETHUSDT']
            lines.append(f"{period} | {cost}배 | {a['completed_trades']}/{a['total_marked_net_usdt']:.2f} | {b['completed_trades']}/{b['total_marked_net_usdt']:.2f} | {result['portfolio']['total_marked_net_usdt']:.2f}")
    lines += ['',f"학습 통계 조건 통과: {len(summary['statistical_pass_train'])}개",f"학습·진단 동시 통과: {len(summary['exploratory_joint_pass'])}개",'실매매 승인 후보: 0개', '',
              '일·월요일 기준 주 묶음을 두 코인과 기준전략에 동일하게 재표집했다.',
              '100,000회, 비교가족 288개, 하위 분위수 0.05/288. 선택 편향을 모두 없애지는 못한다.',
              '최소 코인별 완료거래 100개, 활동 주 20개, 4개 시기 각 15개 및 순익 양수를 요구했다.',
              '이 필요조건에서 탈락한 변형에는 재표집을 생략했고 실패 이유를 기록했다.',
              '선정 조합과 기준전략은 통과 여부와 무관하게 일/주 재표집 하한을 모두 계산했다.', '',
              '장중 유지증거금·청산·부분체결·거래소 보호 주문·재시작 중복주문은 이 데이터로 검증되지 않았다.',
              '일/주 손실 차단 및 실행 중 최대낙폭 차단은 역사 재생에 반영되지 않았다.',
              '월봉·임의 길이 분봉·임의 지표 파라미터까지 모든 가능한 규칙을 탐색했다는 뜻은 아니다.',
              '과거 결과로 미래 수익을 보장할 수 없다.']
    (output.parent/'REPORT_KO.txt').write_text('\n'.join(lines)+'\n')


def run(cache,output,work):
    output.mkdir(parents=True,exist_ok=True)
    frozen=output/'frozen_protocol.json'
    if frozen.exists():raise ValueError('new result directory required')
    definitions=variants();base.write_json(frozen,protocol());digest=base.sha(frozen)
    print('FROZEN',digest,flush=True)
    markets={s:replay.prepare(replay.load_proxy(cache,s)) for s in ('BTCUSDT','ETHUSDT')}
    pools=base.build_pools(markets,work)
    for prepared in markets.values():
        for name,width in WIDTHS.items():
            if name not in prepared['frames']:
                prepared['frames'][name]=replay.aggregate_frame(prepared['market'].minute,width)
    summary={'protocol_sha256':digest,'status':'EXPLORATORY_RESEARCH_NO_LIVE_APPROVAL',
             'source':'SHA-verified Binance USD-M cross-exchange proxy; previously viewed history',
             'variant_count':144,'scenario_count':1152,'periods':{},'exchange_orders_sent':0,
             'untouched_out_of_sample':False,'native_bitget_execution_verified':False,
             'deployment_approved':False,'future_profitability_guaranteed':False,
             'eligible_live_states':0,'selected_rule':None}
    base.BOOT_REPS=BOOT_REPS;base.ALPHA=ALPHA
    selected=None
    for period,start,end in base.PERIODS:
        features={coin:{frame:window_values(prepared['frames'][frame],frame,[p[0] for p in pools[period][coin]])
                        for frame in FRAMES} for coin,prepared in markets.items()}
        print('FEATURES_READY',period,flush=True)
        all_trades={};block={}
        for index,spec in enumerate(definitions):
            filtered={};counts={}
            for coin in markets:
                pairs=pools[period][coin];mask,valid=mask_for(spec,features[coin],pairs)
                filtered[coin]=[p for p,allow in zip(pairs,mask) if allow]
                counts[coin]={'baseline_events':len(pairs),'indicator_valid_events':int(valid.sum()),'indicator_pass_events':int(mask.sum())}
            for fee,personal in (('personal',True),('public',False)):
                for cost in (1,2):
                    key=f"{spec['id']}|{fee}|cost{cost}";trades,rejected=base.portfolio(markets,filtered,cost,personal)
                    all_trades[key]=trades
                    block[key]={'portfolio':base.metrics(trades,start,end),
                                'by_symbol':{coin:base.metrics([r for r in trades if r['symbol']==coin],start,end) for coin in markets},
                                'candidate_counts':counts,'rejected':rejected}
            if (index+1)%12==0:print('PORTFOLIOS_READY',period,index+1,flush=True)
        if selected is None:
            selected,ranking=choose(block,definitions);summary['selected_rule']=selected
            summary['training_only_ranking']=ranking
            base.write_json(output/'selected_rule.json',dict(selected,cci_period=20,cci_long=100,cci_short=-100,chop_period=14,chop_max=38.2,selection_reason='TRAIN_SAMPLE_SUFFICIENT_THEN_DOUBLED_COST_NET',protocol_sha256=digest))
            print('SELECTED',json.dumps(selected),flush=True)
            for cost in (1,2):
                result=block[f"{selected['id']}|personal|cost{cost}"]
                print('SELECTED_TRAIN_COST',cost,json.dumps({c:{k:x[k] for k in ('completed_trades','total_marked_net_usdt','sample_sufficient')} for c,x in result['by_symbol'].items()}),flush=True)
        preconditions={s['id']:necessary_gate(block,s['id']) for s in definitions if s['kind']!='baseline'}
        evaluate={selected['id'],'baseline'}|{name for name,reasons in preconditions.items() if not reasons}
        boot_trades={key:rows for key,rows in all_trades.items() if key.split('|')[0] in evaluate}
        for unit_name,unit in (('day',replay.DAY),('week',replay.WEEK)):
            bounds=base.calendar_bootstrap(boot_trades,start,end,unit)
            for key in boot_trades:
                block[key]['portfolio'].setdefault('bootstrap',{})[unit_name]=bounds[(key,'PORTFOLIO')]
                for coin,stat in block[key]['by_symbol'].items():stat.setdefault('bootstrap',{})[unit_name]=bounds[(key,coin)]
            print('BOOTSTRAP_READY',period,unit_name,'evaluated',len(evaluate),flush=True)
        gates={}
        for name,reasons in preconditions.items():
            if reasons:gates[name]={'passed':False,'failure_reasons':reasons,'bootstrap_required':False,'bootstrap_computed':name in evaluate}
            else:gates[name]=dict(base.variant_gate(block,name),bootstrap_required=True,bootstrap_computed=True)
        for name in (selected['id'],'baseline'):
            for fee in ('personal','public'):
                for cost in (1,2):
                    key=f'{name}|{fee}|cost{cost}';rows=all_trades[key]
                    dd=base.drawdown_proxy(markets,rows,start,end)
                    block[key]['portfolio']['maximum_drawdown_1m_open_proxy']=dd[0]
                    block[key]['portfolio']['minimum_equity_1m_open_proxy_usdt']=dd[1]
                    write_trades(output/f'{period}__{name}__{fee}__cost{cost}_trades.csv',rows)
        summary['periods'][period]={'scenarios':block,'gates':gates,
                                    'necessary_condition_pass':[name for name,reasons in preconditions.items() if not reasons]}
        base.write_json(output/f'{period}_summary.json',summary['periods'][period])
    train=summary['periods'][base.PERIODS[0][0]];diagnostic=summary['periods'][base.PERIODS[1][0]]
    summary['statistical_pass_train']=[n for n,g in train['gates'].items() if g['passed']]
    summary['statistical_pass_diagnostic_repeat']=[n for n,g in diagnostic['gates'].items() if g['passed']]
    summary['exploratory_joint_pass']=[n for n in summary['statistical_pass_train'] if diagnostic['gates'][n]['passed']]
    summary['positive_net_scenario_count']=sum(s['portfolio']['total_marked_net_usdt']>0 for p in summary['periods'].values() for s in p['scenarios'].values())
    base.write_json(output/'summary.json',summary)
    records=[]
    for period,details in summary['periods'].items():
        for key,result in details['scenarios'].items():
            name,fee,cost=key.split('|')
            for coin,stat in [('PORTFOLIO',result['portfolio'])]+list(result['by_symbol'].items()):
                record={'period':period,'variant':name,'fee':fee,'cost':cost,'scope':coin}
                record.update({k:v for k,v in stat.items() if not isinstance(v,(list,dict))})
                for unit in ('day','week'):
                    for metric in ('net_lower_5pct_usdt','net_lower_familywise_usdt','incremental_lower_5pct_usdt','incremental_lower_familywise_usdt'):
                        record[unit+'_'+metric]=stat.get('bootstrap',{}).get(unit,{}).get(metric)
                record['bootstrap_computed']='bootstrap' in stat
                record['gate_pass']=details['gates'].get(name,{}).get('passed',False)
                records.append(record)
    with (output/'comparison_all.csv').open('w',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=list(records[0]));writer.writeheader();writer.writerows(records)
    base.write_json(output/'variant_definitions.json',definitions);report(summary,output)
    print('COMPLETE',json.dumps({'selected':selected,'training_pass':summary['statistical_pass_train'],'joint_pass':summary['exploratory_joint_pass'],'positive_scenarios':summary['positive_net_scenario_count'],'live_pass':0}),flush=True)
    return summary


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cache',required=True,type=Path);parser.add_argument('--output',required=True,type=Path)
    parser.add_argument('--work',required=True,type=Path);args=parser.parse_args()
    run(args.cache,args.output,args.work)


if __name__=='__main__':main()
