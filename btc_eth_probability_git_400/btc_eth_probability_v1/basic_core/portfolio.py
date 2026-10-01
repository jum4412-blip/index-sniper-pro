"""Two independent position machines with one durable account-level owner.

The account is inventoried before any entry. Per-leg API views do not authorize
ownership: the full account check does. All state/trades share one SQLite DB.
"""
import copy,json,sqlite3
from pathlib import Path
from .core import *
from . import account,risk
from .engine import Live as SingleLive

def fresh(c):
    return {'schema':2,'strategy':'probability_price_volume_v1',
            'allocation_usdt':c['allocation_usdt'],'target_notional_usdt':c['target_notional_usdt'],
            'legs':{s:{**risk.fresh(),'cash':c['allocation_usdt']} for s in SYMBOLS},
            'portfolio_guard':risk.fresh(),'account_guard':risk.fresh(),'baseline_account_equity':None,
            'halt':None,'migrated':False}

class LegStore:
    def __init__(self,parent,symbol):self.parent=parent;self.symbol=symbol
    def get(self,key,default=None):
        return self.parent.state['legs'][self.symbol] if key=='engine' else self.parent.store.get(key,default)
    def set(self,key,value):
        if key=='engine':self.parent.state['legs'][self.symbol]=value;self.parent.save()
        else:self.parent.store.set(key,value)
    def event(self,kind,data):self.parent.store.event(kind,{'symbol':self.symbol,**data})
    def seen(self,key):return self.parent.store.seen(key)
    def signal(self,key,data):self.parent.store.signal(key,data)
    def finish(self,t,state):
        old=self.parent.store.db.execute('SELECT json FROM trades WHERE id=?',(t['id'],)).fetchone()
        if old:raise SafetyError('DUPLICATE_TRADE_FINALIZATION')
        state['cash']=number(state['cash'])+number(t['net_usdt'])
        self.parent.state['legs'][self.symbol]=state
        # Re-entry cooldown belongs to each symbol. Loss streaks remain shared.
        global_config={**t['config'],'cooldown_hours':0}
        risk.closed(self.parent.state['portfolio_guard'],t['net_usdt'],global_config,t['closed'])
        risk.closed(self.parent.state['account_guard'],t['net_usdt'],global_config,t['closed'])
        self.parent.store.finish(t,self.parent.state)

class LegAPI:
    def __init__(self,parent,symbol):self.parent=parent;self.symbol=symbol
    def __getattr__(self,k):return getattr(self.parent.api,k)
    def positions(self,category=CAT):
        # Fresh private reads are retained for close-position identity checks.
        return [p for p in self.parent.api.positions(category) if p.get('symbol')==self.symbol]
    def assets(self):return {'usdtEquity':max(.000001,self.parent.leg_equity(self.symbol))}
    def entry_snapshot(self):
        if now_ms()-self.parent.last_refresh>15000:raise DataError('account snapshot stale')
        s=self.parent.snapshot
        return {**s,'positions':[r for r in s['positions'] if r.get('symbol')==self.symbol],
                'orders':[r for r in s['orders'] if r.get('symbol')==self.symbol],
                'strategies':[r for r in s['strategies'] if r.get('symbol')==self.symbol],
                # Keep REAL account collateral for affordability; only the risk
                # equity value is the separate per-leg accounting ledger.
                'assets':s['assets'],'equity':self.assets()['usdtEquity']}
    def settings(self):return self.parent.snapshot['settings']
    def open_orders(self,category=CAT):
        return [r for r in self.parent.snapshot['orders'] if r.get('symbol')==self.symbol and r.get('category',CAT)==category]
    def strategies(self,category=CAT,kind='tpsl'):
        if kind!='tpsl':return []
        return [r for r in self.parent.api.strategies(category,kind) if r.get('symbol')==self.symbol]

class Leg(SingleLive):
    def __init__(self,parent,symbol):
        self.parent=parent;self.symbol=symbol
        super().__init__(LegAPI(parent,symbol),LegStore(parent,symbol),parent.c,
                         lambda:parent.can_enter(symbol),parent.legacy_root,parent.log)
    def boot(self):
        try:
            self.instruments={self.symbol:self.parent.api.instrument(self.symbol)}
        except SafetyError as exc:
            st=self.state;p=st.get('position');pending=st.get('pending')
            source=(p or pending or {}).get('instrument')
            if not source and p:
                source={'symbol':self.symbol,'price_step':p['price_step'],'qty_step':p['qty_step'],'min_qty':p['qty_step'],'min_value':0.,'max_qty':p['original_qty'],'fee':p['fee_rate']}
            if not source:
                if p or pending:raise
                self.instruments={};self.parent.metadata_errors[self.symbol]=str(exc)
                self.parent.store.event('RECOVERY_INSTRUMENT_UNAVAILABLE',{'symbol':self.symbol,'reason':str(exc),'action':'flat leg metadata unavailable; entries blocked, other leg recovery continues'})
                return
            self.instruments={self.symbol:Instrument(**source)}
            self.parent.metadata_errors[self.symbol]=str(exc)
            self.parent.store.event('RECOVERY_INSTRUMENT_UNAVAILABLE',{'symbol':self.symbol,'reason':str(exc),'action':'use frozen entry steps for owned exits; entries blocked'})
        if self.state.get('position'):self.c=copy.deepcopy(self.state['position']['config'])
    def halt(self,reason):
        super().halt(reason);self.parent.halt(reason)
    def entry(self,sig,now):
        # Full-account gate precedes the symbol-filtered view used by SingleLive.
        self.parent.refresh(True)
        if not self.parent.can_enter(self.symbol):return False
        return super().entry(sig,now)

class Portfolio:
    def __init__(self,api,store,c,authorized,legacy_root,trend_root,log=None,basic_root=None,psych_root=None,trail3_root=None):
        self.api=api;self.store=store;self.c=c;self.authorized=authorized
        self.legacy_root=Path(legacy_root);self.trend_root=Path(trend_root);self.log=log or (lambda *_:None)
        self.basic_root=Path(basic_root) if basic_root else None
        self.psych_root=Path(psych_root) if psych_root else None
        self.trail3_root=Path(trail3_root) if trail3_root else None
        self.metadata_errors={}
        self.state=store.get('engine',fresh(c));self.snapshot=None;self.quotes={};self.last_full=0;self.last_refresh=0
        if self.state.get('schema')!=2 or self.state.get('strategy')!='probability_price_volume_v1' or set(self.state.get('legs',{}))!=set(SYMBOLS):raise SafetyError('INCOMPATIBLE_DATABASE')
        if (self.state.get('allocation_usdt')!=c['allocation_usdt']
                or self.state.get('target_notional_usdt')!=c['target_notional_usdt']):
            raise SafetyError('INCOMPATIBLE_SIZING_DATABASE: use a new installation; preserve the prior live database')
        self.legs={s:Leg(self,s) for s in SYMBOLS}
    def save(self):self.store.set('engine',self.state)
    def halt(self,why):
        if not self.state.get('halt'):
            self.state['halt']=why;self.save();self.store.event('HALT',{'reason':why});self.log('HALT',{'reason':why})
    def boot(self):
        uid=account.identity(self.api,self.store)
        if not self.state['migrated']:
            old,old_uid=risk.old_state(self.legacy_root)
            sources=[('Larry',old,old_uid)]
            for label,old_root in [('trend',self.trend_root),('basic',self.basic_root),('psych',self.psych_root),('ETH Larry Trail3',self.trail3_root)]:
                if old_root is None:continue
                p=old_root/'data/live.sqlite'
                if p.exists():
                    with sqlite3.connect(p.resolve().as_uri()+'?mode=ro',uri=True) as db:
                        def val(k):
                            r=db.execute('SELECT json FROM state WHERE key=?',(k,)).fetchone();return json.loads(r[0]) if r else None
                        old=val('engine') or {}
                        for leg in old.get('legs',{}).values():
                            if leg.get('position') or leg.get('pending'):raise SafetyError('LEGACY_POSITION_OR_PENDING')
                        if 'account_guard' in old:
                            guard=copy.deepcopy(old['account_guard'])
                            if old.get('halt'):guard['halt']=old['halt']
                            old=guard
                        sources.append((label,old,val('account_uid')))
            guard=self.state['account_guard']
            for name,old,old_uid in sources:
                if old_uid and str(old_uid)!=uid:raise SafetyError('LEGACY_ACCOUNT_MISMATCH')
                if any(old.get(k) for k in ('position','pending','managed_position','pending_entry')):raise SafetyError('LEGACY_POSITION_OR_PENDING')
                if not old:continue
                normalized=risk.fresh();risk.inherit(normalized,old);old={**old,**normalized}
                # Preserve worst applicable legacy account guards; never scale an
                # old whole-account peak down to the new allocated trading budget.
                for k in ('peak_equity','max_observed_drawdown_pct','loss_pause_until','cooldown_until','consecutive_losses'):
                    if k in old:guard[k]=max(guard.get(k,0),number(old[k]))
                for label in ('day','week'):
                    key=label+'_key'
                    if old.get(key) and (not guard.get(key) or old[key]>guard[key]):
                        for suffix in ('key','start_equity','blocked'):
                            if label+'_'+suffix in old:guard[label+'_'+suffix]=old[label+'_'+suffix]
                    elif old.get(key)==guard.get(key):
                        guard[label+'_start_equity']=max(guard.get(label+'_start_equity',0),old.get(label+'_start_equity',0))
                        guard[label+'_blocked']=guard.get(label+'_blocked',False) or old.get(label+'_blocked',False)
                if old.get('halt'):guard['halt']='LEGACY_HALT: '+str(old['halt'])
            self.state['migrated']=True;self.save()
        for leg in self.legs.values():leg.boot()
        try:self.refresh(True)
        except SafetyError as exc:
            # Recovery must keep reconciling a durable intent even when the
            # exchange's position/attached-stop snapshots arrive out of order.
            if not any(leg.state.get('position') or leg.state.get('pending') for leg in self.legs.values()):raise
            self.store.event('RECOVERY_SNAPSHOT_DEFERRED',{'entries_enabled':False,'reason':str(exc)})
        self.store.event('BOOT',{'mode':'LIVE','symbols':list(SYMBOLS),'allocation_per_symbol':self.c['allocation_usdt']})
    def leg_equity(self,symbol):
        st=self.state['legs'][symbol];p=st.get('position');value=number(st['cash'])
        if p:
            q=self.quotes.get(symbol)
            if q and 0<=now_ms()-q.ts<=8000:
                value+=(1 if p['side']=='LONG' else -1)*(q.exit(p['side'])-p['entry'])*p['qty']
            else:
                # Unknown prices are not realised losses. Subtracting the new
                # risk ceiling here would falsely latch daily losses on restart.
                return max(.000001,number(st.get('equity',value)))
            value-=abs(p['entry_fee']) if p.get('entry_fee') is not None else p['entry']*p['qty']*p['fee_rate']
        return value
    def _expected(self,symbol):
        leg=self.legs[symbol];st=leg.state;p=st.get('position');pending=st.get('pending')
        if p:return p
        if pending and pending['kind']=='ENTRY':
            inst=leg.instruments[symbol];sig=pending['signal'];s=pending['sized']
            return {'symbol':symbol,'side':sig['side'],'qty':s['qty'],'original_qty':s['qty'],
                    'initial_stop':s['stop'],'price_step':inst.price_step,'qty_step':inst.qty_step,'opened':pending['created']}
        return None
    def validate_inventory(self,snap):
        identities=set();allowed=set();actual_qty={};propagating=set()
        for pos in snap['positions']:
            symbol=pos.get('symbol');p=self._expected(symbol) if symbol in SYMBOLS else None
            if not p or pos.get('category',CAT)!=CAT or str(pos.get('posSide','')).upper()!=p['side']:raise SafetyError('UNMANAGED_EXCHANGE_POSITION')
            if symbol in identities:raise SafetyError('DUPLICATE_SYMBOL_POSITION')
            identities.add(symbol);actual_qty[symbol]=number(pos.get('total'),True)
            if number(pos.get('total'),True)>p['original_qty']+p['qty_step']/2:raise SafetyError('POSITION_SIZE_CHANGED')
            if pos.get('createdTime') and abs(int(pos['createdTime'])-p['opened'])>10000:raise SafetyError('POSITION_IDENTITY_CHANGED')
            if pos.get('marginMode')!='crossed' or number(pos.get('leverage'))!=5:raise SafetyError('POSITION_SETTINGS_CHANGED')
        for symbol in SYMBOLS:
            p=self._expected(symbol)
            if p:
                pending=self.state['legs'][symbol].get('pending')
                if pending and pending['kind']=='ENTRY' and symbol in actual_qty:
                    p={**p,'qty':actual_qty[symbol]}
                ids=account.protection(snap['strategies'],p);allowed.update(ids)
                live_position=self.state['legs'][symbol].get('position')
                if live_position:
                    # A fresh full-account snapshot must replace cached coverage
                    # before authorizing the other symbol's entry.
                    live_position['protection_ids']=ids
                    live_position['protection_checked']=now_ms()
                    if ids and not live_position.get('bound_protection_ids'):
                        live_position['bound_protection_ids']=ids
                    exiting=bool(pending and pending['kind']=='EXIT') or live_position.get('closing')
                    if symbol in actual_qty and not ids and not exiting and now_ms()>live_position['protect_deadline']:
                        live_position['force_exit']='SAFETY_NO_PROTECTION'
                        self.halt('EXCHANGE_STOP_MISSING')
                    self.save()
                elif pending and pending['kind']=='ENTRY' and symbol not in actual_qty:
                    # A partial IOC's attached stop may propagate before the
                    # position and fill history. Defer the snapshot; do not adopt
                    # this order or claim that requested size is protected.
                    for order in snap['strategies']:
                        oid=str(order.get('orderId',''))
                        qty=number(order.get('qty',0))
                        if oid not in allowed and 0<qty<=p['qty']+p['qty_step']/2:
                            probe={**p,'qty':qty}
                            if account.protection([order],probe):propagating.add(oid)
                if pending and pending['kind']=='ENTRY' and symbol in actual_qty and not ids and now_ms()-pending['created']>15000:
                    pending['force_exit']='SAFETY_NO_PROTECTION'
                    self.halt('PENDING_ENTRY_STOP_MISSING: inspect exchange while fills reconcile')
                    self.save()
        for order in snap['orders']:
            pending=self.state['legs'].get(order.get('symbol'),{}).get('pending')
            if not pending or order.get('category',CAT)!=CAT or not (order.get('clientOid')==pending['cid'] or str(order.get('orderId',''))==pending.get('oid') or (pending.get('safety_exit') and (order.get('clientOid')==pending['safety_exit']['cid'] or str(order.get('orderId',''))==pending['safety_exit'].get('oid')))):raise SafetyError('UNMANAGED_OPEN_ORDER')
        for order in snap['strategies']:
            if str(order.get('orderId','')) not in allowed|propagating:raise SafetyError('UNMANAGED_STRATEGY_ORDER')
        if propagating:raise DataError('PENDING_ENTRY_STRATEGY_PROPAGATION')
        for key in ('debt','totalDebt','usdtDebt'):
            if key in snap['assets'] and number(snap['assets'][key])>0:raise SafetyError('ACCOUNT_BORROWING_PRESENT')
    def refresh(self,full=False):
        now=now_ms()
        try:
            for symbol in list(self.metadata_errors):
                try:self.legs[symbol].instruments[symbol]=self.api.instrument(symbol);self.metadata_errors.pop(symbol,None)
                except SafetyError:pass
            self.state['entry_metadata_errors']=dict(self.metadata_errors)
            if full or not self.snapshot or now-self.last_full>60000:
                snap=account.inventory(self.api);self.last_full=now
            else:
                snap=copy.deepcopy(self.snapshot)
                snap['positions']=[r for r in snap['positions'] if r.get('category',CAT)!=CAT]+self.api.positions()
                snap['orders']=[r for r in snap['orders'] if r.get('category',CAT)!=CAT]+self.api.open_orders()
                snap['assets']=self.api.assets();snap['equity']=number(snap['assets']['usdtEquity'],True)
                # Protection is checked by each leg with fresh private reads.
                snap['strategies']=self.api.strategies()
            self.validate_inventory(snap)
            available=account.available_usdt(snap['assets'])
            if self.state['baseline_account_equity'] is None:
                account.require_flat(snap)
                if snap['equity']<self.c['min_account_equity_usdt'] or available<self.c['min_account_equity_usdt']:
                    raise SafetyError('INSUFFICIENT_910_USDT_WITH_RESERVE')
                self.state['baseline_account_equity']=snap['equity']
            self.snapshot=snap;self.last_refresh=now_ms()
            eq=len(SYMBOLS)*self.c['allocation_usdt']+snap['equity']-self.state['baseline_account_equity']
            if eq<=0:self.halt('ALLOCATED_EQUITY_EXHAUSTED');eq=.000001
            portfolio_blocks=risk.blocks(self.state['portfolio_guard'],eq,self.c,now_ms())
            account_blocks=risk.blocks(self.state['account_guard'],snap['equity'],self.c,now_ms())
            for b in portfolio_blocks+account_blocks:
                if b=='MAX_DRAWDOWN' or b.startswith('LEGACY_HALT'):self.halt(b)
            if any(b in ('MAX_DRAWDOWN','DAILY_LOSS_LIMIT','WEEKLY_LOSS_LIMIT') for b in portfolio_blocks+account_blocks):
                for leg in self.legs.values():
                    if leg.state.get('position'):leg.state['position']['force_exit']='ACCOUNT_DRAWDOWN_EMERGENCY' if 'MAX_DRAWDOWN' in portfolio_blocks+account_blocks else 'ACCOUNT_PERIOD_LOSS_LIMIT'
            self.state['equity']=eq;self.state['account_equity']=snap['equity'];self.state['available_usdt']=available
            self.state['entry_blocks']=list(dict.fromkeys(portfolio_blocks+account_blocks));self.save()
        except SafetyError as exc:
            self.last_refresh=0
            if not isinstance(exc,DataError) and str(exc)!='INSUFFICIENT_910_USDT_WITH_RESERVE':self.halt(str(exc))
            raise
    def can_enter(self,symbol):
        if (self.trend_root/'data/LIVE_ENABLED.json').exists():return False
        if self.basic_root and (self.basic_root/'data/LIVE_ENABLED.json').exists():return False
        if self.psych_root and (self.psych_root/'data/LIVE_ENABLED.json').exists():return False
        if self.trail3_root and (self.trail3_root/'data/LIVE_ENABLED.json').exists():return False
        if not self.authorized() or self.state.get('halt') or not self.last_refresh or now_ms()-self.last_refresh>15000:return False
        if self.state.get('entry_blocks') or self.metadata_errors:return False
        # Serialize uncertain intents, but permit two confirmed live positions.
        if any(leg.state.get('pending') for leg in self.legs.values()):return False
        for other in self.legs.values():
            if other.state.get('halt') and other.state['halt']!='MAX_DRAWDOWN':return False
            p=other.state.get('position')
            if p and (not p.get('protection_ids') or p.get('force_exit') or p.get('closing')):return False
        leg=self.legs[symbol]
        return not leg.state.get('position') and not leg.state.get('halt')
    def poll(self,now):
        errors=[]
        for symbol,leg in self.legs.items():
            try:leg.poll(now_ms())
            except (SafetyError,KeyError,ValueError,TypeError) as exc:errors.append((symbol,exc))
        if now_ms()-self.last_refresh>=15000:
            try:self.refresh()
            except (SafetyError,KeyError,ValueError,TypeError) as exc:errors.append(('ACCOUNT',exc))
        if errors:raise errors[0][1]
    def tick(self,q,f,now):
        self.quotes[q.symbol]=q;self.legs[q.symbol].tick(q,f,now)
    def entry(self,sig,now):return self.legs[sig['symbol']].entry(sig,now)
