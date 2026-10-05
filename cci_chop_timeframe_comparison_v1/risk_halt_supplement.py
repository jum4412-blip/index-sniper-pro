"""Selected rule only: causal minute-open account entry-halt approximation.

The selected grid is not re-ranked. This separate experiment adds the runtime's
daily/week/peak equity tests and sticky halt when a new eligible entry is denied.
It remains a proxy for 3-second Bitget mark-price/effective-equity observations.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import numpy as np

from . import runner

replay=runner.replay


def portfolio(markets,pools,cost,personal,start,end):
    n=(end-start)//replay.MIN
    changes=np.zeros(n+1);marks=np.zeros(n+1)
    active=[];trades=[];rejected=Counter()
    day_start={};week_start={};day_net=Counter();week_net=Counter()
    cursor=-1;balance=replay.ACCOUNT_SNAPSHOT;peak=balance;halt=None

    def advance(now_idx):
        nonlocal cursor,balance,peak
        if now_idx<=cursor:return balance+marks[now_idx]
        a=cursor+1;b=now_idx+1
        cash=balance+np.cumsum(changes[a:b]);curve=cash+marks[a:b]
        peak=max(peak,float(curve.max()))
        for unit,origin,target in ((replay.DAY,0,day_start),(replay.WEEK,replay.WEEK_ORIGIN,week_start)):
            lo=(start+a*replay.MIN-origin)//unit;hi=(start+now_idx*replay.MIN-origin)//unit
            for key in range(lo,hi+1):
                if key not in target:
                    first=max(a,int((origin+key*unit-start)//replay.MIN))
                    target[key]=float(curve[first-a])
        balance=float(cash[-1]);cursor=now_idx
        return balance+marks[now_idx]

    def schedule(row):
        nonlocal balance
        market=markets[row['symbol']]['market'];sign=1 if row['side']=='long' else -1
        a=(row['entry_ms']-start)//replay.MIN;release=min(n,(row['exit_ms']-start)//replay.MIN+1)
        offset=(start-int(market.minute[0,0]))//replay.MIN
        prices=market.minute[offset+a:offset+release,1]
        marks[a:release]+=sign*(prices-row['entry'])*row['qty']
        before=float(changes[a]);changes[a]-=row['entry_fees_usdt']
        events=market.funding
        lower=np.searchsorted(events[:,0],row['entry_ms'],'left')
        boundary=row['exit_ms']+(replay.MIN if row['exit_intraminute'] else 0)
        upper=np.searchsorted(events[:,0],boundary,'left' if row['exit_intraminute'] else 'right')
        used=events[lower:upper]
        indices=np.searchsorted(market.minute[:,0],used[:,0],'right')-1
        cash=-sign*row['qty']*used[:,1]*market.minute[indices,1]
        ambiguous=(used[:,0]==row['entry_ms'])|(used[:,0]>=row['exit_ms'])
        cash[ambiguous]=np.minimum(cash[ambiguous],0)
        cash=np.where(cash<0,cash*cost,cash)
        if not np.isclose(cash.sum(),row['funding_usdt'],atol=1e-7):raise ValueError('funding schedule mismatch')
        for idx,value in zip(((used[:,0]-start)//replay.MIN).astype(int),cash):
            if 0<=idx<=n:changes[idx]+=value
        changes[release]+=row['net_usdt']+row['entry_fees_usdt']-row['funding_usdt']
        # New accepted entry costs can occur at the minute already advanced.
        balance+=float(changes[a])-before

    merged=sorted([pair for pairs in pools.values() for pair in pairs],key=lambda p:(p[0]['entry_ms'],-p[0]['score'],p[0]['symbol']))
    for signal,path in merged:
        now=signal['entry_ms'];idx=(now-start)//replay.MIN
        equity=advance(idx)
        completed=[row for row in active if row['exit_ms']<now]
        for row in completed:
            if not row['censored']:
                day_net[(row['exit_ms'])//replay.DAY]+=row['net_usdt']
                week_net[(row['exit_ms']-replay.WEEK_ORIGIN)//replay.WEEK]+=row['net_usdt']
        active=[row for row in active if row['exit_ms']>=now]
        if halt is not None:rejected['persistent_account_risk_halt']+=1;continue
        if len(active)>=2 or any(row['symbol']==signal['symbol'] for row in active):rejected['position_limit']+=1;continue
        day=now//replay.DAY;week=(now-replay.WEEK_ORIGIN)//replay.WEEK
        blocks=[]
        for key,bucket,starts,fraction,label in ((day,day_net,day_start,.02,'DAILY_LOSS_LIMIT'),(week,week_net,week_start,.05,'WEEKLY_LOSS_LIMIT')):
            initial=starts[key]
            if min(bucket[key],equity-initial)<=-fraction*initial:blocks.append(label)
        if equity<=peak*.9:blocks.append('ACCOUNT_DRAWDOWN_LIMIT')
        if blocks:
            halt={'entry_ms':now,'entry_utc':replay.utc(now),'reasons':blocks,'equity_usdt':equity,'peak_usdt':peak}
            rejected['account_risk_limit']+=1;continue
        row=replay.materialize(markets[signal['symbol']],signal,path,5,cost,personal,equity)
        if row is None:rejected['size_or_risk']+=1;continue
        if sum(r['planned_guard_risk_usdt'] for r in active)+row['planned_guard_risk_usdt']>equity*replay.PORTFOLIO_RISK_FRACTION+1e-9:
            rejected['aggregate_risk']+=1;continue
        if equity-sum(r['margin_usdt'] for r in active)<row['margin_usdt']+replay.RESERVE+1e-9:
            rejected['cash_reserve']+=1;continue
        row['exit_intraminute']=path['intraminute'];schedule(row)
        trades.append(row);active.append(row)
    advance(n)
    curve=replay.ACCOUNT_SNAPSHOT+np.cumsum(changes)+marks
    expected=replay.ACCOUNT_SNAPSHOT+sum(r['net_usdt'] for r in trades)
    if not np.isclose(curve[-1],expected,atol=1e-6):raise ValueError('terminal equity reconciliation failed')
    peaks=np.maximum.accumulate(np.r_[replay.ACCOUNT_SNAPSHOT,curve])[1:]
    dd=float(((peaks-curve)/peaks).max())
    return trades,dict(rejected),halt,(dd,float(curve.min()))


def main():
    here=Path(__file__).parent;output=here/'risk_halt_results';output.mkdir()
    selected=json.loads((here/'results'/'selected_rule.json').read_text())
    protocol={'experiment':'SELECTED_CCI_CHOP_RUNTIME_ENTRY_HALT_APPROXIMATION_V1',
              'created_utc':datetime.now(timezone.utc).isoformat(),'source_sha256':runner.base.sha(__file__),
              'selected_rule':selected,'original_grid_protocol_sha256':selected['protocol_sha256'],
              'selection_changed':False,'costs':[1,2],'fees':['personal','public'],'scenario_count':8,
              'daily_loss_fraction':.02,'weekly_loss_fraction':.05,'peak_drawdown_fraction':.10,
              'halt_policy':'sticky on eligible new signal while any account risk block active; no automatic reset',
              'account_observation':'1m opens; entry fees, conservative funding schedule, proxy exits settle next minute',
              'mode':'historical cross-exchange proxy only; not identical to actual mark/effective equity or native fills',
              'native_execution_verified':False,'deployment_approved':False}
    runner.base.write_json(output/'frozen_protocol.json',protocol)
    markets={s:replay.prepare(replay.load_proxy(here.parent/'btc_eth_15m_backtest'/'cache',s)) for s in ('BTCUSDT','ETHUSDT')}
    pools=runner.base.build_pools(markets,here/'_work')
    for p in markets.values():
        for f in (selected['cci_frame'],selected['chop_frame']):
            if f not in p['frames']:p['frames'][f]=replay.aggregate_frame(p['market'].minute,runner.WIDTHS[f],replay.WEEK_ORIGIN if f=='W' else 0)
    summary={'protocol_sha256':runner.base.sha(output/'frozen_protocol.json'),'selected_rule':selected,'scenarios':{},
             'selection_changed':False,'live_equivalence_proven':False,'deployment_approved':False}
    for period,start,end in runner.base.PERIODS:
        filtered={}
        for coin,p in markets.items():
            pairs=pools[period][coin]
            features={f:runner.window_values(p['frames'][f],f,[pair[0] for pair in pairs]) for f in (selected['cci_frame'],selected['chop_frame'])}
            mask,_=runner.mask_for(selected,features,pairs);filtered[coin]=[pair for pair,allow in zip(pairs,mask) if allow]
        for fee,personal in (('personal',True),('public',False)):
            for cost in (1,2):
                trades,rejected,halt,dd=portfolio(markets,filtered,cost,personal,start,end)
                key=f'{period}|{fee}|cost{cost}'
                summary['scenarios'][key]={'portfolio':runner.base.metrics(trades,start,end,dd),
                                          'by_symbol':{c:runner.base.metrics([r for r in trades if r['symbol']==c],start,end) for c in markets},
                                          'rejected':rejected,'persistent_halt':halt}
                runner.write_trades(output/(key.replace('|','__')+'_trades.csv'),trades)
                print('HALT_REPLAY',key,json.dumps(summary['scenarios'][key]),flush=True)
    runner.base.write_json(output/'summary.json',summary)


if __name__=='__main__':main()
