"""No reselection: paired calendar bounds for already chosen guarded portfolios."""
from datetime import datetime, timezone
import csv
import json
from pathlib import Path

from . import runner


def main():
    here=Path(__file__).parent;output=here/'guarded_grid_results'
    summary=json.loads((output/'summary.json').read_text())
    names=['baseline',summary['original_selected_rule']['id'],summary['guarded_training_selected_rule']['id']]
    protocol={'experiment':'GUARDED_ALREADY_SELECTED_CALENDAR_BOOTSTRAP_V1',
              'created_utc':datetime.now(timezone.utc).isoformat(),'source_sha256':runner.base.sha(__file__),
              'guarded_grid_protocol_sha256':summary['protocol_sha256'],'names':names,
              'bootstrap_repetitions':runner.BOOT_REPS,'seed':runner.base.BOOT_SEED,
              'corrected_lower_quantile':runner.ALPHA,'family_count':288,
              'reselection':False,'identical_calendar_draws_all_coins_and_baseline':True}
    runner.base.write_json(output/'bootstrap_protocol.json',protocol)
    runner.base.BOOT_REPS=runner.BOOT_REPS;runner.base.ALPHA=runner.ALPHA
    result={'protocol_sha256':runner.base.sha(output/'bootstrap_protocol.json'),'periods':{}}
    for period,start,end in runner.base.PERIODS:
        ledgers={}
        for name in names:
            for fee in ('personal','public'):
                for cost in (1,2):
                    key=f'{name}|{fee}|cost{cost}';path=output/f'{period}__{name}__{fee}__cost{cost}_trades.csv'
                    with path.open() as f:rows=list(csv.DictReader(f))
                    ledgers[key]=[dict(symbol=r['symbol'],exit_ms=int(r['exit_ms']),net_usdt=float(r['net_usdt']),censored=r['censored']=='True') for r in rows]
        period_bounds={}
        for unit_name,unit in (('day',runner.replay.DAY),('week',runner.replay.WEEK)):
            bounds=runner.base.calendar_bootstrap(ledgers,start,end,unit)
            period_bounds[unit_name]={f'{key}|{coin}':value for (key,coin),value in bounds.items()}
            print('GUARDED_BOOTSTRAP',period,unit_name,flush=True)
        result['periods'][period]=period_bounds
    runner.base.write_json(output/'bootstrap_bounds.json',result)


if __name__=='__main__':main()
