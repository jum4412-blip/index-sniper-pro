"""Second frozen comparison: all same placements with runtime entry-halt proxy.

This experiment follows viewing the original grid and selected-rule risk test.
It is not untouched validation and does not silently replace the first selection.
"""
from __future__ import annotations

from datetime import datetime, timezone
import csv
import json
from pathlib import Path

from . import runner
from . import risk_halt_supplement as risk


def main():
    here=Path(__file__).parent;output=here/'guarded_grid_results';output.mkdir()
    definitions=runner.variants();original=json.loads((here/'results'/'selected_rule.json').read_text())
    protocol={'experiment':'ALL_CCI_CHOP_PLACEMENTS_ACCOUNT_ENTRY_HALT_APPROXIMATION_V1',
              'created_utc':datetime.now(timezone.utc).isoformat(),
              'source_sha256':{p.name:runner.base.sha(p) for p in (Path(__file__),Path(risk.__file__),Path(runner.__file__))},
              'source_hashes':runner.replay.PROXY_HASHES,'original_selected_rule':original,
              'variants':definitions,'variant_count':144,'scenario_count':1152,
              'cci_chop_thresholds_changed':False,'weekly_daily_structure_exits_changed':False,
              'risk':{'daily_loss_fraction':.02,'weekly_loss_fraction':.05,'drawdown_fraction':.10,
                      'sticky_halt_on_eligible_new_signal':True,'mark_equity_observation':'1m opens'},
              'selection':'same original sample-sufficient/train2cost/train1cost/id rank; combined121 only; independent supplementary ranking, original selection not replaced',
              'fresh_untouched_data':False,'original_grid_and_selected8_risk_results_already_viewed':True,
              'native_mark_equity_and_fills_equivalence':False,'deployment_approved':False,
              'frozen_before_guarded_all_placement_results':True}
    runner.base.write_json(output/'frozen_protocol.json',protocol)
    markets={s:runner.replay.prepare(runner.replay.load_proxy(here.parent/'btc_eth_15m_backtest'/'cache',s)) for s in ('BTCUSDT','ETHUSDT')}
    pools=runner.base.build_pools(markets,here/'_work')
    for prepared in markets.values():
        for frame,width in runner.WIDTHS.items():
            if frame not in prepared['frames']:prepared['frames'][frame]=runner.replay.aggregate_frame(prepared['market'].minute,width)
    summary={'protocol_sha256':runner.base.sha(output/'frozen_protocol.json'),'variant_count':144,'scenario_count':1152,
             'original_selection_changed':False,'original_selected_rule':original,'periods':{},
             'deployment_approved':False,'native_execution_verified':False,'live_eligible_states':0}
    selected=None
    for period,start,end in runner.base.PERIODS:
        features={coin:{frame:runner.window_values(p['frames'][frame],frame,[pair[0] for pair in pools[period][coin]])
                        for frame in runner.FRAMES} for coin,p in markets.items()}
        block={};ledgers={}
        for fee,personal in (('personal',True),('public',False)):
            for index,spec in enumerate(definitions):
                filtered={}
                for coin in markets:
                    mask,_=runner.mask_for(spec,features[coin],pools[period][coin])
                    filtered[coin]=[pair for pair,allow in zip(pools[period][coin],mask) if allow]
                for cost in (1,2):
                    key=f"{spec['id']}|{fee}|cost{cost}"
                    trades,rejected,halt,dd=risk.portfolio(markets,filtered,cost,personal,start,end)
                    block[key]={'portfolio':runner.base.metrics(trades,start,end,dd),
                                'by_symbol':{coin:runner.base.metrics([r for r in trades if r['symbol']==coin],start,end) for coin in markets},
                                'rejected':rejected,'persistent_halt':halt}
                    ledgers[key]=trades
                if (index+1)%12==0:print('GUARDED_READY',period,fee,index+1,flush=True)
            if selected is None:
                selected,ranking=runner.choose(block,definitions)
                summary['guarded_training_selected_rule']=selected;summary['guarded_training_ranking']=ranking
                runner.base.write_json(output/'guarded_selected_rule.json',dict(selected,protocol_sha256=summary['protocol_sha256'],sample_sufficient=all(x['sample_sufficient'] for cost in (1,2) for x in block[f"{selected['id']}|personal|cost{cost}"]['by_symbol'].values()),original_selection_replaced=False))
                print('GUARDED_SELECTED',json.dumps(selected),flush=True)
        gates={s['id']:{'necessary_failure_reasons':runner.necessary_gate(block,s['id']),
                        'necessary_conditions_passed':not runner.necessary_gate(block,s['id']),
                        'statistical_pass_claimed':False} for s in definitions if s['kind']!='baseline'}
        names={'baseline',original['id'],selected['id']}
        for name in names:
            for fee in ('personal','public'):
                for cost in (1,2):
                    key=f'{name}|{fee}|cost{cost}'
                    runner.write_trades(output/f'{period}__{name}__{fee}__cost{cost}_trades.csv',ledgers[key])
        summary['periods'][period]={'scenarios':block,'necessary_gates':gates}
        runner.base.write_json(output/f'{period}_summary.json',summary['periods'][period])
        runner.base.write_json(output/'partial_summary.json',summary)
    summary['positive_net_scenario_count']=sum(x['portfolio']['total_marked_net_usdt']>0 for p in summary['periods'].values() for x in p['scenarios'].values())
    summary['necessary_condition_pass_both_periods']=[name for name,g in summary['periods'][runner.base.PERIODS[0][0]]['necessary_gates'].items()
                                                    if g['necessary_conditions_passed'] and summary['periods'][runner.base.PERIODS[1][0]]['necessary_gates'][name]['necessary_conditions_passed']]
    rows=[]
    for period,p in summary['periods'].items():
        for key,result in p['scenarios'].items():
            name,fee,cost=key.split('|')
            for coin,stat in [('PORTFOLIO',result['portfolio'])]+list(result['by_symbol'].items()):
                row={'period':period,'variant':name,'fee':fee,'cost':cost,'scope':coin}
                row.update({k:v for k,v in stat.items() if not isinstance(v,(list,dict))})
                row['persistent_halt']=result['persistent_halt'] is not None
                row['halt_utc']=result['persistent_halt'].get('entry_utc') if result['persistent_halt'] else None
                row['necessary_conditions_passed']=p['necessary_gates'].get(name,{}).get('necessary_conditions_passed',False)
                rows.append(row)
    with (output/'comparison_all.csv').open('w',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
    runner.base.write_json(output/'summary.json',summary)
    print('GUARDED_COMPLETE',json.dumps({'selected':selected,'positive_scenarios':summary['positive_net_scenario_count'],'necessary_both_periods':summary['necessary_condition_pass_both_periods']}),flush=True)


if __name__=='__main__':main()
