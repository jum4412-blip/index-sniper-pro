"""Run all frozen recipes in a NEW directory, preserving published results."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cache',required=True,type=Path,help='directory containing four original hash-checked NPZs')
    parser.add_argument('--destination','--output',dest='destination',required=True,type=Path,help='new, nonexistent directory')
    args=parser.parse_args()
    archive=args.cache.resolve();target=args.destination.resolve();source=Path(__file__).resolve().parent
    if target==source or source in target.parents:
        raise SystemExit('destination must be outside the original project directory')
    if target.exists():raise SystemExit('destination already exists; choose a NEW directory')
    required=('BTCUSDT_minute.npz','BTCUSDT_funding.npz','ETHUSDT_minute.npz','ETHUSDT_funding.npz')
    if not all((archive/name).is_file() for name in required):raise SystemExit('four original NPZ files are required')
    target.mkdir(parents=True)
    copied=target/source.name
    shutil.copytree(source,copied,ignore=shutil.ignore_patterns('results','risk_halt_results','guarded_grid_results','__pycache__','*.pyc','*.log','REPORT_KO.txt','VERIFIED_RESULTS.json'))
    data=target/'btc_eth_15m_backtest';data.mkdir();(data/'cache').symlink_to(archive,target_is_directory=True)
    env=dict(os.environ,OPENBLAS_NUM_THREADS='1',OMP_NUM_THREADS='1')
    prefix=[sys.executable,'-u','-m']
    for command in (
        prefix+[source.name+'.runner','--cache',str(archive),'--output',str(copied/'results'),'--work',str(copied/'_work')],
        prefix+[source.name+'.risk_halt_supplement'],
        prefix+[source.name+'.guarded_grid'],
        prefix+[source.name+'.guarded_bootstrap'],
    ):
        subprocess.run(command,cwd=target,env=env,check=True)
    folder=copied/'guarded_grid_results';summary=json.loads((folder/'summary.json').read_text())
    rule=dict(summary['guarded_training_selected_rule'],cci_period=20,cci_long=100,cci_short=-100,chop_period=14,chop_max=38.2,
              selection_reason='GUARDED_TRAIN_SAMPLE_SUFFICIENT_THEN_DOUBLED_COST_NET',
              protocol_sha256=summary['protocol_sha256'],sample_sufficient=False,statistically_approved=False,original_selection_replaced=False)
    (folder/'guarded_selected_rule.json').write_text(json.dumps(rule,ensure_ascii=False,indent=2)+'\n')
    print('Reproduced results:',copied)


if __name__=='__main__':main()
