"""Persistent equity guards. Loss limits are not reset by a restart or installation."""
from datetime import datetime, timezone
from pathlib import Path
import json, sqlite3
from .core import SafetyError, number

def fresh():
    return {'position':None,'pending':None,'halt':None,'consecutive_losses':0,
            'loss_pause_until':0,'cooldown_until':0}

def old_state(root):
    root=Path(root).expanduser().resolve()
    p=root/'data/larry_v2/live.sqlite'
    state={};uid=None
    if p.is_file():
        with sqlite3.connect(p.as_uri()+'?mode=ro',uri=True) as db:
            r=db.execute("SELECT json FROM state WHERE key='live'").fetchone()
            if r:state=json.loads(r[0])
            r=db.execute("SELECT json FROM state WHERE key='account_uid'").fetchone()
            if r:uid=str(json.loads(r[0]))
    if not state:
        p=root/'data/larry_williams_core_v1_state.json'
        if p.is_file():state=json.loads(p.read_text())
    return state,uid

def legacy_arms(root):
    root=Path(root).expanduser().resolve()
    paths=[root/'data/larry_v2/ARM.json',root/'data/LARRY_WILLIAMS_CORE_V1_ARMED.json']
    cfg=root/'config/larry_williams_core_v1.json'
    if cfg.is_file():
        target=(root/str(json.loads(cfg.read_text()).get('arm_path','data/LARRY_WILLIAMS_CORE_V1_ARMED.json'))).resolve()
        if not target.is_relative_to(root):raise SafetyError('legacy authorization path outside project')
        paths.append(target)
    return sorted(set(paths))

def legacy_check(root):
    if any(p.exists() for p in legacy_arms(root)):
        raise SafetyError('LEGACY_ARMED: run ./trend pause-legacy; existing positions remain managed')
    s,_=old_state(root)
    if any(s.get(k) for k in ('position','pending','managed_position','pending_entry')):
        raise SafetyError('LEGACY_POSITION_OR_PENDING: wait for the previous bot to reconcile')

def inherit(state,old):
    for key in ('day_key','day_start_equity','week_key','week_start_equity','peak_equity',
                'consecutive_losses','loss_pause_until','max_observed_drawdown_pct','day_blocked','week_blocked','cooldown_until'):
        if key in old:state[key]=old[key]
    if isinstance(old.get('cooldown'),dict) and old['cooldown']:
        state['cooldown_until']=max(state.get('cooldown_until',0),*old['cooldown'].values())
    if 'max_drawdown_pct' in old:
        state['max_observed_drawdown_pct']=max(state.get('max_observed_drawdown_pct',0),number(old['max_drawdown_pct']))
    if old.get('halt'):state['halt']='LEGACY_HALT: '+str(old['halt'])

def blocks(state,equity,c,now):
    equity=number(equity,True)
    dt=datetime.fromtimestamp(now/1000,timezone.utc)
    day=dt.strftime('%Y-%m-%d');iy,iw,_=dt.isocalendar();week=f'{iy}-W{iw:02d}'
    for label,key in (('day',day),('week',week)):
        if state.get(label+'_key')!=key:
            state[label+'_key']=key;state[label+'_start_equity']=equity
            state[label+'_blocked']=False
        number(state.get(label+'_start_equity'),True)
    state['equity']=equity
    peak=max(number(state.get('peak_equity',equity),True),equity)
    state['peak_equity']=peak
    dd=max(0,(peak-equity)/peak*100)
    state['max_observed_drawdown_pct']=max(state.get('max_observed_drawdown_pct',0),dd)
    if dd>=c['max_drawdown_pct']:state['halt']=state.get('halt') or 'MAX_DRAWDOWN'
    for label,limit in (('day','daily_loss_pct'),('week','weekly_loss_pct')):
        base=state[label+'_start_equity']
        if (base-equity)/base*100>=c[limit]:state[label+'_blocked']=True
    reasons=[]
    if state.get('halt'):reasons.append(str(state['halt']))
    if state.get('day_blocked'):reasons.append('DAILY_LOSS_LIMIT')
    if state.get('week_blocked'):reasons.append('WEEKLY_LOSS_LIMIT')
    if now<state.get('loss_pause_until',0):reasons.append('CONSECUTIVE_LOSS_PAUSE')
    if now<state.get('cooldown_until',0):reasons.append('COOLDOWN')
    return reasons

def closed(state,net,c,now):
    net=number(net)
    state['consecutive_losses']=state.get('consecutive_losses',0)+1 if net<0 else 0
    if state['consecutive_losses']>=c['max_consecutive_losses']:
        state['loss_pause_until']=now+int(c['loss_pause_hours']*3_600_000)
    state['cooldown_until']=now+int(c['cooldown_hours']*3_600_000)
