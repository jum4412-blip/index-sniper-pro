"""Single-position state machine. An uncertain write is reconciled, never replayed."""
import copy, uuid
from dataclasses import replace
from .core import *
from .api import fill_summary
from . import account, risk, strategy

TERMINAL={'filled','cancelled','canceled','rejected','failed'}

class Live:
    def __init__(self,api,store,c,authorized,legacy_root,log=None):
        self.api=api;self.store=store;self.c=c;self.authorized=authorized;self.legacy_root=legacy_root
        self.log=log or (lambda kind,data:None)
        self.state=store.get('engine',risk.fresh())
        self.last_poll=0;self.last_assets=0;self.last_protection=0;self.last_notice={}
        self.instruments={}
    def save(self):self.store.set('engine',self.state)
    def event(self,kind,data):
        self.store.event(kind,data);self.log(kind,data)
    def notice(self,kind,data,interval=60):
        now=now_ms()
        if now-self.last_notice.get(kind,0)>=interval*1000:
            self.last_notice[kind]=now;self.event(kind,data)
    def halt(self,reason):
        if not self.state.get('halt'):
            self.state['halt']=reason;self.save();self.event('HALT',{'reason':reason})
    def boot(self):
        uid=account.identity(self.api,self.store)
        if not self.store.get('risk_migrated'):
            old,old_uid=risk.old_state(self.legacy_root)
            if old_uid and uid!=old_uid:raise SafetyError('LEGACY_ACCOUNT_MISMATCH')
            risk.inherit(self.state,old);self.save()
            self.store.set('risk_migrated',{'at':now_ms(),'source':'existing local Larry state' if old else 'new account baselines'})
        for symbol in SYMBOLS:self.instruments[symbol]=self.api.instrument(symbol)
        if self.state.get('position'):
            # Preserve the entry-time parameters across configuration edits.
            self.c=copy.deepcopy(self.state['position']['config'])
        self.event('BOOT',{'mode':'LIVE','position':bool(self.state.get('position')),
                           'pending':bool(self.state.get('pending')),'halt':self.state.get('halt')})
    def entry(self,sig,now):
        if self.store.seen(sig['key']):return False
        self.store.signal(sig['key'],{'signal':sig,'decision':'CONSUMED_ONCE'})
        if self.state.get('position') or self.state.get('pending'):return False
        if not self.authorized():return False
        from .cli import legacy_process_guard
        legacy_process_guard()
        risk.legacy_check(self.legacy_root)
        snap=self.api.entry_snapshot() if hasattr(self.api,'entry_snapshot') else account.inventory(self.api)
        account.require_flat(snap)
        b=risk.blocks(self.state,snap['equity'],self.c,now_ms());self.save()
        if b:raise SafetyError(','.join(b))
        if not account.correct_settings(snap['settings'],sig['symbol']):raise SafetyError('CROSS_5X_NOT_CONFIRMED')
        inst=self.api.instrument(sig['symbol'])
        self.instruments[sig['symbol']]=inst
        q=self.api.quote(sig['symbol'])
        settled=self.api.settled_funding(sig['symbol'],now_ms())
        sign=1 if sig['side']=='LONG' else -1
        # Upcoming ticker rate can veto an entry but never substitutes for settled data.
        if sign*q.funding>self.c['max_adverse_funding_rate']:raise SafetyError('UPCOMING_FUNDING_EXTRA_VETO')
        q=replace(q,funding=settled['rate'])
        sized=strategy.size(sig,q,inst,self.c,snap['equity'],now_ms())
        free_margin=account.available_usdt(snap['assets'])
        # A short's IOC floor is below its quote; reserve against the sized
        # quote/limit maximum instead of understating required margin.
        notional=number(sized.get('notional',number(sized['qty'],True)*number(sized['limit'],True)),True)
        if notional>self.c['target_notional_usdt']+1e-8:
            raise SafetyError('SIZED_NOTIONAL_EXCEEDS_TARGET')
        required=notional/self.c['leverage']+2*notional*number(sized['fee_rate'])+self.c['margin_reserve_usdt']
        if free_margin+1e-8<required:raise SafetyError('INSUFFICIENT_AVAILABLE_MARGIN_WITH_RESERVE')
        sized['required_free_margin_usdt']=required
        cid='PBE_'+uuid.uuid4().hex[:26]
        body={'category':CAT,'symbol':sig['symbol'],'side':'buy' if sig['side']=='LONG' else 'sell',
              'qty':ds(sized['qty']),'orderType':'limit','price':ds(sized['limit']),'timeInForce':'ioc',
              'clientOid':cid,'marginMode':'crossed','stopLoss':ds(sized['stop']),
              'slTriggerBy':'mark','slOrderType':'market'}
        hold=snap['settings']['holdMode']
        if hold=='hedge_mode':body['posSide']=sig['side'].lower()
        else:body['reduceOnly']='no'
        # Check authorization again after the potentially slow whole-account inventory.
        risk.legacy_check(self.legacy_root)
        if not self.authorized():raise SafetyError('PAUSED_DURING_PREFLIGHT')
        legacy_process_guard()
        pending={'kind':'ENTRY','cid':cid,'body':body,'signal':sig,'sized':sized,'created':now_ms(),
                 'equity':snap['equity'],'hold_mode':hold,'config':copy.deepcopy(self.c),
                 'instrument':copy.deepcopy(vars(inst)),'settled_funding':settled}
        self.state['pending']=pending;self.save()
        self.event('ENTRY_DISPATCH',{'cid':cid,'symbol':sig['symbol'],'side':sig['side'],**sized})
        self._send(pending)
        return True
    def _send(self,pending):
        try:
            response=self.api.post('/api/v3/trade/place-order',pending['body'])
            if not isinstance(response,dict) or not response.get('orderId'):
                raise UnknownOrder('missing orderId; pending retained')
            pending['oid']=str(response['orderId']);self.save()
        except UnknownOrder:
            self.event('ORDER_UNKNOWN',{'kind':pending['kind'],'cid':pending['cid'],'action':'NO_RESEND'})
        except Rejected as exc:
            # Only an explicit, non-ambiguous exchange rejection can clear the intent.
            self.event('ORDER_REJECTED',{'kind':pending['kind'],'cid':pending['cid'],'reason':str(exc)})
            self.state['pending']=None
            if pending['kind']=='EXIT':
                p=self.state['position'];p['next_exit_attempt']=now_ms()+30_000
                p['rejected_exits']=p.get('rejected_exits',0)+1
                self.halt('EXIT_REJECTED_REVIEW_REQUIRED')
            self.save()
    def reconcile_pending(self,now):
        pending=self.state.get('pending')
        if not pending:return
        if pending.get('safety_exit') and not self.reconcile_safety_exit(pending,now):return
        try:info=self.api.order(pending.get('oid',''),pending['cid'])
        except SafetyError:
            self.notice('ORDER_RECONCILIATION_PENDING',{'cid':pending['cid'],'action':'NO_RESEND'})
            return
        if not isinstance(info,dict):raise DataError('order detail missing')
        if info.get('symbol')!=pending['body']['symbol']:raise SafetyError('ORDER_SYMBOL_MISMATCH')
        if info.get('clientOid') and info['clientOid']!=pending['cid']:raise SafetyError('ORDER_CLIENT_ID_MISMATCH')
        if pending.get('oid') and info.get('orderId') and str(info['orderId'])!=pending['oid']:
            raise SafetyError('ORDER_ID_MISMATCH')
        if info.get('category') and info['category']!=CAT:raise SafetyError('ORDER_CATEGORY_MISMATCH')
        if info.get('side') and info['side']!=pending['body']['side']:raise SafetyError('ORDER_SIDE_MISMATCH')
        status=str(info.get('orderStatus','')).lower()
        reported=number(info.get('cumExecQty',0))
        if reported<0 or reported>number(pending['body']['qty'],True)+self.instruments[pending['body']['symbol']].qty_step/2:raise SafetyError('REPORTED_FILL_ABOVE_AUTHORIZED_QUANTITY')
        if reported+1e-12<number(pending.get('reported_cum_exec_qty',0)):
            self.halt('ENTRY_CUMULATIVE_FILL_REGRESSED');raise DataError('ENTRY_CUMULATIVE_FILL_REGRESSED')
        pending['reported_cum_exec_qty']=reported;self.save()
        if status not in TERMINAL:
            if status not in ('live','new','partially_filled'):raise DataError('unknown order status')
            if now-pending['created']>10_000 and not pending.get('cancel_sent'):
                pending['cancel_sent']=True;self.save()
                try:self.api.post('/api/v3/trade/cancel-order',{'category':CAT,'clientOid':pending['cid']})
                except SafetyError:pass
            return
        qty=number(info.get('cumExecQty'))
        if qty<0:raise DataError('negative execution quantity')
        if qty==0:
            self.state['pending']=None
            if pending['kind']=='EXIT':
                self.state['position']['next_exit_attempt']=now+30_000
                self.state['position']['rejected_exits']=self.state['position'].get('rejected_exits',0)+1
                self.halt('EXIT_UNFILLED_REVIEW_REQUIRED')
            self.save();self.event('ORDER_UNFILLED',{'cid':pending['cid']});return
        oid=str(info.get('orderId') or pending.get('oid') or '')
        if not oid:raise DataError('filled order id missing')
        fills=self.api.fills(oid)
        for f in fills:
            if str(f.get('orderId'))!=oid or f.get('symbol')!=pending['body']['symbol']:
                raise SafetyError('FILL_OWNERSHIP_MISMATCH')
        summary=fill_summary(fills)
        inst=self.instruments[pending['body']['symbol']]
        if summary is None or abs(summary['qty']-qty)>inst.qty_step/2:return
        requested=number(pending['body']['qty'],True)
        if qty>requested+inst.qty_step/2:raise SafetyError('FILLED_ABOVE_AUTHORIZED_QUANTITY')
        if pending['kind']=='EXIT':
            p=self.state['position']
            p['qty']=float(max(Decimal('0'),Decimal(str(pending['pre_qty']))-Decimal(str(qty))))
            p.setdefault('exit_orders',[]).append({'oid':oid,'cid':pending['cid'],'qty':qty,'fills':fills})
            p['exit_reason']=pending['reason'];p['closing']=True
            p['next_exit_attempt']=now+3000
            self.state['pending']=None;self.save()
            self.event('EXIT_FILLED',{'cid':pending['cid'],'qty':qty,'remaining':p['qty']})
            return
        s=pending['sized'];sig=pending['signal'];entry=summary['price'];sign=1 if sig['side']=='LONG' else -1
        gap=sign*(entry-s['stop'])
        p={'id':pending['cid'],'symbol':sig['symbol'],'side':sig['side'],'opened':summary['first'],
           'entry':entry,'qty':qty,'original_qty':qty,'initial_stop':s['stop'],'stop':s['stop'],
           'initial_r':max(abs(entry-s['stop']),inst.price_step),'price_step':inst.price_step,'qty_step':inst.qty_step,
           'entry_oid':oid,'entry_fills':fills,'entry_fee':summary['fees'],'entry_equity':pending['equity'],
           'fee_rate':s['fee_rate'],'hold_mode':pending['hold_mode'],'protect_deadline':now+15_000,
           'mfe_r':0.,'mae_r':0.,'risk_budget':s['risk_budget'],'config':pending['config'],
           'instrument':pending.get('instrument'),'settled_funding_at_entry':pending.get('settled_funding'),
           'signal':sig,'exit_bar':sig['bar_end'],'rejected_exits':0,'force_exit':pending.get('force_exit')}
        p['exchange_stop']=s['stop']
        p['protection_mode']='EXCHANGE_INITIAL_STOP_LOCAL_2R_TIME_EXIT'
        p['target']=rounded(entry+sign*2*p['initial_r'],inst.price_step,up=sign==1)
        p['expires']=p['opened']+int(pending['config']['max_holding_minutes']*60000)
        p['last_quote_at']=now
        p['local_trail_stale']=False
        cfg=pending['config']
        worst_exit=s['stop']*(1-sign*cfg['exit_slippage_bps']/10000)
        fill_risk=qty*(gap+abs(worst_exit-s['stop'])+s['fee_rate']*(entry+worst_exit))
        p['initial_risk_usdt']=fill_risk if gap>0 else None
        p['entry_notional_usdt']=qty*entry
        cap=number(cfg.get('max_trade_risk_usdt',s['risk_budget']),True)
        if gap<=0 or fill_risk>min(s['risk_budget'],cap)+1e-8:
            self.state['halt']=self.state.get('halt') or 'POST_FILL_RISK_EXCEEDED'
            p['force_exit']='SAFETY_RISK_EXIT'
        # A sell IOC may receive a better (higher) price, very slightly raising
        # executed value. Planned size remains capped; allow one explicit price
        # tolerance only, never an additional position or a margin multiplier.
        notional_tolerance=1+max(number(cfg.get('entry_slippage_bps',5)),1)/10000
        p['notional_fill_tolerance']=notional_tolerance
        if 'target_notional_usdt' in cfg and qty*entry>number(cfg['target_notional_usdt'],True)*notional_tolerance+1e-6:
            self.state['halt']=self.state.get('halt') or 'POST_FILL_NOTIONAL_EXCEEDED'
            p['force_exit']='SAFETY_NOTIONAL_EXIT'
        if pending.get('safety_closed_qty'):
            p['qty']=max(0,qty-pending['safety_closed_qty']);p['closing']=True;p['exit_reason']='SAFETY_UNPROTECTED_PARTIAL'
            p['exit_orders']=pending.get('safety_exit_orders',[])
        self.state['position']=p;self.state['pending']=None;self.save()
        self.event('OPEN',{'symbol':p['symbol'],'side':p['side'],'qty':qty,'entry':entry,'stop':p['stop'],
                           'initial_risk_usdt':p['initial_risk_usdt'],'target':p['target'],'expires':p['expires'],
                           'probability':sig.get('frame',{}).get('probability',{}),
                           'event_type':(sig.get('frame',{}).get('event') or {}).get('event_type'),
                           'force_exit':p.get('force_exit')})
    def reconcile_safety_exit(self,pending,now):
        """A child emergency exit has its own durable id; ambiguous writes never retry."""
        child=pending['safety_exit']
        if child.get('rejected'):return False
        try:info=self.api.order(child.get('oid',''),child['cid'])
        except SafetyError:
            self.notice('ORDER_RECONCILIATION_PENDING',{'cid':child['cid'],'action':'NO_RESEND'});return False
        if not isinstance(info,dict):raise DataError('safety exit detail missing')
        if info.get('symbol')!=pending['body']['symbol'] or info.get('clientOid')!=child['cid']:raise SafetyError('SAFETY_EXIT_OWNERSHIP_MISMATCH')
        if info.get('side') and info['side']!=child['body']['side']:raise SafetyError('SAFETY_EXIT_SIDE_MISMATCH')
        if child.get('oid') and str(info.get('orderId',''))!=child['oid']:raise SafetyError('SAFETY_EXIT_ORDER_ID_MISMATCH')
        if info.get('category') and info['category']!=CAT:raise SafetyError('SAFETY_EXIT_CATEGORY_MISMATCH')
        status=str(info.get('orderStatus','')).lower()
        if status not in TERMINAL:
            if status not in ('live','new','partially_filled'):raise DataError('unknown safety exit status')
            return False
        qty=number(info.get('cumExecQty'))
        if not 0<=qty<=number(child['body']['qty'])+self.instruments[pending['body']['symbol']].qty_step/2:raise SafetyError('SAFETY_EXIT_FILL_QUANTITY_MISMATCH')
        if qty>0:
            oid=str(info.get('orderId') or child.get('oid') or '')
            if not oid:raise DataError('safety fill order id missing')
            fills=self.api.fills(oid)
            if any(f.get('symbol')!=pending['body']['symbol'] or str(f.get('orderId'))!=oid for f in fills):raise SafetyError('SAFETY_EXIT_FILL_OWNERSHIP_MISMATCH')
            summary=fill_summary(fills)
            if summary is None or abs(summary['qty']-qty)>self.instruments[pending['body']['symbol']].qty_step/2:return False
            pending['safety_closed_qty']=pending.get('safety_closed_qty',0)+qty
            pending.setdefault('safety_exit_orders',[]).append({'oid':oid,'cid':child['cid'],'qty':qty,'fills':fills})
        pending['safety_exit']=None;pending['safety_next_attempt']=now+3000;self.save()
        return True

    def protect_pending_partial(self,positions,now):
        pending=self.state.get('pending')
        if not pending or pending['kind']!='ENTRY' or now-pending['created']<=15000:return
        sig=pending['signal'];inst=self.instruments[sig['symbol']]
        probe={'symbol':sig['symbol'],'side':sig['side'],'opened':pending['created'],'qty':pending['sized']['qty'],
               'original_qty':pending['sized']['qty'],'qty_step':inst.qty_step,'price_step':inst.price_step,'initial_stop':pending['sized']['stop']}
        pos=account.matching(positions,probe)
        if pos is None:return
        qty=number(pos.get('total'),True);reported=number(pending.get('reported_cum_exec_qty',0))
        if abs(qty-max(0,reported-pending.get('safety_closed_qty',0)))>inst.qty_step/2 or qty>probe['original_qty']+inst.qty_step/2:raise SafetyError('PENDING_PARTIAL_OWNERSHIP_QUANTITY_UNRESOLVED')
        if not pos.get('createdTime') or abs(int(pos['createdTime'])-pending['created'])>10000:raise SafetyError('PENDING_PARTIAL_POSITION_IDENTITY_UNRESOLVED')
        probe['qty']=qty
        if account.protection(self.api.strategies(),probe):return
        pending['force_exit']='SAFETY_UNPROTECTED_PARTIAL';self.halt('PENDING_ENTRY_STOP_MISSING');self.save()
        if pending.get('safety_exit') or now<pending.get('safety_next_attempt',0):return
        # Only confirmed exchange quantity is reduced. No entry quantity is guessed.
        cid='PBS_'+uuid.uuid4().hex[:26]
        body={'category':CAT,'symbol':sig['symbol'],'side':'sell' if sig['side']=='LONG' else 'buy','qty':ds(qty),'orderType':'market','clientOid':cid,'marginMode':'crossed'}
        if pending['hold_mode']=='hedge_mode':body['posSide']=sig['side'].lower()
        else:body['reduceOnly']='yes'
        child={'cid':cid,'body':body,'created':now};pending['safety_exit']=child;self.save()
        self.event('EXIT_DISPATCH',{'cid':cid,'symbol':sig['symbol'],'reason':'SAFETY_UNPROTECTED_PARTIAL','qty':qty})
        try:
            response=self.api.post('/api/v3/trade/place-order',body)
            if not isinstance(response,dict) or not response.get('orderId'):raise UnknownOrder('safety exit acknowledgement missing')
            child['oid']=str(response['orderId']);self.save()
        except UnknownOrder:self.event('ORDER_UNKNOWN',{'kind':'SAFETY_EXIT','cid':cid,'action':'NO_RESEND'})
        except Rejected:
            child['rejected']=True;self.save();self.event('URGENT_EXIT_REJECTED',{'symbol':sig['symbol'],'action':'inspect exchange; safety child rejected'})

    def close_position(self,reason,now):
        p=self.state.get('position')
        if not p or self.state.get('pending') or now<p.get('next_exit_attempt',0):return
        if p.get('rejected_exits',0)>=3:
            self.notice('URGENT_EXIT_REJECTED',{'symbol':p['symbol'],'action':'inspect exchange; automatic retries stopped'})
            return
        if p['qty']<=p['qty_step']/2:return
        pos=account.matching(self.api.positions(),p)
        if pos is None:return
        if abs(number(pos['total'])-p['qty'])>p['qty_step']/2:
            self.halt('POSITION_SIZE_CHANGED');return
        if pos.get('createdTime') and abs(int(pos['createdTime'])-p['opened'])>10_000:
            self.halt('POSITION_IDENTITY_CHANGED');return
        cid='PBX_'+uuid.uuid4().hex[:26]
        body={'category':CAT,'symbol':p['symbol'],'side':'sell' if p['side']=='LONG' else 'buy',
              'qty':ds(p['qty']),'orderType':'market','clientOid':cid,'marginMode':'crossed'}
        if p['hold_mode']=='hedge_mode':body['posSide']=p['side'].lower()
        else:body['reduceOnly']='yes'
        pending={'kind':'EXIT','cid':cid,'body':body,'created':now,'reason':reason,'pre_qty':p['qty']}
        p['closing']=True;p['exit_reason']=reason
        self.state['pending']=pending;self.save()
        self.event('EXIT_DISPATCH',{'cid':cid,'symbol':p['symbol'],'reason':reason,'qty':p['qty']})
        self._send(pending)
    def finish_position(self,p,now):
        try:result=account.history_match(self.api,p,now)
        except SafetyError as exc:
            self.halt(str(exc));return
        if result is None:
            p.setdefault('flat_seen',now);self.save()
            if now-p['flat_seen']>120_000:self.halt('CLOSED_POSITION_ACCOUNTING_UNRESOLVED')
            self.notice('AWAITING_EXCHANGE_HISTORY',{'symbol':p['symbol']});return
        initial_risk=p.get('initial_risk_usdt',p['risk_budget'])
        t={**p,**result,'reason':p.get('exit_reason','EXCHANGE_OR_EXTERNAL_CLOSE'),
           'net_r':result['net_usdt']/initial_risk if initial_risk else None,'mode':'LIVE'}
        risk.closed(self.state,t['net_usdt'],p['config'],now)
        self.state['position']=None
        self.store.finish(t,self.state)
        self.event('CLOSE',{'symbol':t['symbol'],'net_usdt':t['net_usdt'],'funding_usdt':t['funding_usdt'],
                            'reason':t['reason'],'net_r':t['net_r']})
    def poll(self,now):
        if now-self.last_poll<5000:return
        self.last_poll=now
        self.reconcile_pending(now)
        positions=self.api.positions()
        self.protect_pending_partial(positions,now)
        p=self.state.get('position')
        if p:
            if now>=p.get('expires',p['opened']+int(p['config']['max_holding_minutes']*60000)):
                p['force_exit']=p.get('force_exit') or 'MAX_HOLDING_60_MINUTES'
            stale=now-p.get('last_quote_at',p['opened'])>self.c.get('max_tick_gap_seconds',30)*1000
            p['local_trail_stale']=stale
            if stale:
                self.notice('LOCAL_TRAIL_STALE',{'symbol':p['symbol'],
                    'exchange_stop':p['initial_stop'],'local_stop':p['stop'],
                    'action':'original structural stop remains on exchange; local trailing awaits fresh quotes'})
            pos=account.matching(positions,p)
            if pos is None:
                if not self.state.get('pending') and now>p['protect_deadline']:
                    self.finish_position(p,now)
                return
            if abs(number(pos['total'])-p['qty'])>p['qty_step']/2:
                # Position propagation can trail a known terminal exit fill briefly.
                if p.get('closing') and now<p.get('next_exit_attempt',0)+10_000:return
                self.halt('POSITION_SIZE_CHANGED');return
            if len(positions)!=1:self.halt('UNMANAGED_ADDITIONAL_POSITION')
            if pos.get('marginMode')!='crossed' or number(pos.get('leverage'))!=5:
                self.halt('POSITION_SETTINGS_CHANGED')
            if not self.state.get('pending') and now-self.last_protection>=15_000:
                self.last_protection=now
                ids=account.protection(self.api.strategies(),p)
                p['protection_checked']=now;p['protection_ids']=ids
                if ids and not p.get('bound_protection_ids'):p['bound_protection_ids']=ids
                if not ids and now>p['protect_deadline']:
                    self.halt('EXCHANGE_STOP_MISSING');p['force_exit']='SAFETY_NO_PROTECTION'
                self.save()
            if p.get('force_exit') or p.get('closing'):
                self.close_position(p.get('force_exit') or p.get('exit_reason','EXIT_REMAINDER'),now)
        elif positions and not self.state.get('pending'):
            self.halt('UNMANAGED_EXCHANGE_POSITION')
        if now-self.last_assets>=15_000:
            self.last_assets=now
            equity=number(self.api.assets().get('usdtEquity'),True)
            b=risk.blocks(self.state,equity,self.c,now);self.save()
            if self.state.get('position') and any(x in ('MAX_DRAWDOWN','DAILY_LOSS_LIMIT','WEEKLY_LOSS_LIMIT') for x in b):
                self.state['position']['force_exit']='ACCOUNT_LOSS_LIMIT';self.save()
                self.close_position('ACCOUNT_LOSS_LIMIT',now)
    def tick(self,q,f,now):
        p=self.state.get('position')
        if not p or p['symbol']!=q.symbol:return
        q.validate(now)
        previous=p['stop'];sign=1 if p['side']=='LONG' else -1
        reason=p.get('force_exit') or strategy.manage(p,q,f,p['config'],now)
        if sign*(p['stop']-previous)<-p['price_step']/2:
            p['stop']=previous;self.halt('LOCAL_TRAIL_LOOSENED');p['force_exit']='SAFETY_TRAIL_EXIT'
        p['last_quote_at']=q.ts;p['local_trail_stale']=False
        self.save()
        if reason:self.close_position(reason,now)
