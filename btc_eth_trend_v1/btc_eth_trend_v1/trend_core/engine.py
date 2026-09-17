"""Single-position state machine. An uncertain write is reconciled, never replayed."""
import copy, uuid
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
        snap=account.inventory(self.api);account.require_flat(snap)
        b=risk.blocks(self.state,snap['equity'],self.c,now_ms());self.save()
        if b:raise SafetyError(','.join(b))
        if not account.correct_settings(snap['settings'],sig['symbol']):raise SafetyError('CROSS_5X_NOT_CONFIRMED')
        inst=self.api.instrument(sig['symbol']);self.instruments[sig['symbol']]=inst
        q=self.api.quote(sig['symbol'])
        sized=strategy.size(sig,q,inst,self.c,snap['equity'],now_ms())
        cid='TCE_'+uuid.uuid4().hex[:26]
        body={'category':CAT,'symbol':sig['symbol'],'side':'buy' if sig['side']=='LONG' else 'sell',
              'qty':ds(sized['qty']),'orderType':'limit','price':ds(sized['limit']),'timeInForce':'ioc',
              'clientOid':cid,'marginMode':'crossed','reduceOnly':'no','stopLoss':ds(sized['stop']),
              'slTriggerBy':'mark','slOrderType':'market'}
        hold=snap['settings']['holdMode']
        if hold=='hedge_mode':body['posSide']=sig['side'].lower()
        # Check authorization again after the potentially slow whole-account inventory.
        risk.legacy_check(self.legacy_root)
        if not self.authorized():raise SafetyError('PAUSED_DURING_PREFLIGHT')
        legacy_process_guard()
        pending={'kind':'ENTRY','cid':cid,'body':body,'signal':sig,'sized':sized,'created':now_ms(),
                 'equity':snap['equity'],'hold_mode':hold,'config':copy.deepcopy(self.c)}
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
        try:info=self.api.order(pending.get('oid',''),pending['cid'])
        except SafetyError:
            self.notice('ORDER_RECONCILIATION_PENDING',{'cid':pending['cid'],'action':'NO_RESEND'})
            return
        if not isinstance(info,dict):raise DataError('order detail missing')
        if info.get('symbol')!=pending['body']['symbol']:raise SafetyError('ORDER_SYMBOL_MISMATCH')
        if info.get('clientOid') and info['clientOid']!=pending['cid']:raise SafetyError('ORDER_CLIENT_ID_MISMATCH')
        status=str(info.get('orderStatus','')).lower()
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
           'signal':sig,'exit_bar':sig['bar_end'],'rejected_exits':0}
        worst_exit=s['stop']*(1-sign*self.c['exit_slippage_bps']/10000)
        fill_risk=qty*(gap+abs(worst_exit-s['stop'])+s['fee_rate']*(entry+worst_exit))
        p['initial_risk_usdt']=fill_risk if gap>0 else None
        if gap<=0 or fill_risk>s['risk_budget']*1.02:
            self.state['halt']=self.state.get('halt') or 'POST_FILL_RISK_EXCEEDED'
            p['force_exit']='SAFETY_RISK_EXIT'
        self.state['position']=p;self.state['pending']=None;self.save()
        self.event('OPEN',{'symbol':p['symbol'],'side':p['side'],'qty':qty,'entry':entry,'stop':p['stop'],
                           'initial_risk_usdt':p['initial_risk_usdt'],'force_exit':p.get('force_exit')})
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
        cid='TCX_'+uuid.uuid4().hex[:26]
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
        p=self.state.get('position')
        if p:
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
            if self.state.get('position') and any(x in b for x in ('DAILY_LOSS_LIMIT','WEEKLY_LOSS_LIMIT','MAX_DRAWDOWN')):
                self.state['position']['force_exit']='ACCOUNT_LOSS_LIMIT';self.save()
                self.close_position('ACCOUNT_LOSS_LIMIT',now)
    def tick(self,q,f,now):
        p=self.state.get('position')
        if not p or p['symbol']!=q.symbol:return
        reason=strategy.manage(p,q,f,p['config'],now);self.save()
        if reason:self.close_position(reason,now)

class Paper:
    """Forward simulation using observed executable quotes; no private API client."""
    def __init__(self,api,store,c,log=None):
        self.api=api;self.store=store;self.c=c;self.log=log or (lambda *_:None)
        self.state=store.get('engine',risk.fresh());self.instruments={}
        self.state.setdefault('cash',c['paper_seed'])
    def save(self):self.store.set('engine',self.state)
    def boot(self):
        for sym in SYMBOLS:self.instruments[sym]=self.api.instrument(sym)
        if self.state.get('position'):
            # Unknown intraperiod price path cannot be reconstructed from a restart.
            self.state['position']['sample_gap']=True
            self.c=copy.deepcopy(self.state['position']['config'])
        self.save()
    def poll(self,now):
        if not self.state.get('position'):
            risk.blocks(self.state,self.state['cash'],self.c,now);self.save()
    def entry(self,sig,now):
        if self.store.seen(sig['key']):return False
        self.store.signal(sig['key'],sig)
        if self.state.get('position') or risk.blocks(self.state,self.state['cash'],self.c,now):self.save();return False
        q=self.api.quote(sig['symbol']);inst=self.instruments[sig['symbol']]
        s=strategy.size(sig,q,inst,self.c,self.state['cash'],now_ms())
        # Conservative quote fill at the worst permitted IOC limit. Liquidity/queue
        # are not simulated, therefore this is never execution proof.
        p={'id':'P_'+uuid.uuid4().hex,'symbol':sig['symbol'],'side':sig['side'],'opened':now,
           'entry':s['limit'],'qty':s['qty'],'original_qty':s['qty'],'initial_stop':s['stop'],'stop':s['stop'],
           'initial_r':abs(s['limit']-s['stop']),'price_step':inst.price_step,'qty_step':inst.qty_step,
           'entry_equity':self.state['cash'],'fee_rate':s['fee_rate'],'risk_budget':s['risk_budget'],
           'config':copy.deepcopy(self.c),'mfe_r':0.,'mae_r':0.,'last_tick':now,'sample_gap':False}
        self.state['cash']-=p['entry']*p['qty']*p['fee_rate']
        self.state['position']=p;self.save();self.log('PAPER_OPEN',{'symbol':p['symbol'],'qty':p['qty']})
        return True
    def tick(self,q,f,now):
        p=self.state.get('position')
        if not p or p['symbol']!=q.symbol:return
        if now-p.get('last_tick',now)>30_000:p['sample_gap']=True
        p['last_tick']=now
        sign=1 if p['side']=='LONG' else -1
        equity=self.state['cash']+sign*(q.exit(p['side'])-p['entry'])*p['qty']
        b=risk.blocks(self.state,equity,self.c,now)
        reason=strategy.manage(p,q,f,p['config'],now)
        if any(x in b for x in ('DAILY_LOSS_LIMIT','WEEKLY_LOSS_LIMIT','MAX_DRAWDOWN')):reason='ACCOUNT_LOSS_LIMIT'
        if reason:
            exit_price=q.exit(p['side'])*(1-sign*self.c['exit_slippage_bps']/10000)
            gross=sign*(exit_price-p['entry'])*p['qty']
            fee=(exit_price+p['entry'])*p['qty']*p['fee_rate'];net=gross-fee
            self.state['cash']+=gross-exit_price*p['qty']*p['fee_rate']
            t={**p,'closed':now,'exit':exit_price,'gross_usdt':gross,'fees_usdt':fee,
               'net_before_funding_usdt':net,'funding_usdt':None,'net_usdt':None,'reason':reason,
               'accounting':'SIMULATED_QUOTES_FUNDING_UNKNOWN','mode':'PAPER'}
            risk.closed(self.state,net,self.c,now)
            self.state['position']=None;self.store.finish(t,self.state)
            self.log('PAPER_CLOSE',{'symbol':t['symbol'],'net_before_funding_usdt':net,'sample_gap':t['sample_gap']})
        else:self.save()
