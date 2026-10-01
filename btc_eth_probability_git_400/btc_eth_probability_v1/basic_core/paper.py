"""Durable forward paper trading on public quotes, never private credentials.

This is a quote-sampled model: no order-book depth, missed intratick excursions,
funding settlement, or liquidation simulation. Trades report net BEFORE funding,
not verified exchange net P&L. Entry/exit each pay fee and adverse slippage once.
"""
import copy,json,uuid
from .core import *
from . import strategy,risk

class Paper:
    def __init__(self,api,store,c,authorized=lambda:True,log=None):
        self.api=api;self.store=store;self.c=c;self.authorized=authorized;self.log=log or (lambda *_:None)
        default={'paper_schema':1,'allocation_usdt':c['allocation_usdt'],'target_notional_usdt':c['target_notional_usdt'],
                 'legs':{s:{**risk.fresh(),'cash':c['allocation_usdt']} for s in SYMBOLS},
                 'portfolio_guard':risk.fresh(),'halt':None,'equity':2*c['allocation_usdt'],
                 'account_equity':c['min_account_equity_usdt'],'cash_reserve':c['min_account_equity_usdt']-2*c['allocation_usdt'],'entry_blocks':[],'funding_accounting':'UNMODELED'}
        self.state=store.get('engine',default);self.instruments={};self.quotes={}
        if self.state.get('paper_schema')!=1 or set(self.state.get('legs',{}))!=set(SYMBOLS):raise SafetyError('INCOMPATIBLE_PAPER_DATABASE')
        if (self.state.get('allocation_usdt')!=c['allocation_usdt']
                or self.state.get('target_notional_usdt')!=c['target_notional_usdt']):
            raise SafetyError('INCOMPATIBLE_PAPER_SIZING_DATABASE: use a new paper installation')
    def save(self):self.store.set('engine',self.state)
    def event(self,kind,data):self.store.event(kind,data);self.log(kind,data)
    def boot(self):
        for s in SYMBOLS:
            self.instruments[s]=self.api.instrument(s)
            p=self.state['legs'][s].get('position')
            if p:p['sample_gap']=True
        self.save();self.event('BOOT',{'mode':'PAPER','funding':'UNMODELED','liquidation':'NOT_SIMULATED'})
    def halt(self,reason):
        if not self.state.get('halt'):self.state['halt']=reason;self.save();self.event('HALT',{'reason':reason})
    def leg_equity(self,symbol):
        st=self.state['legs'][symbol];p=st.get('position');value=number(st['cash'])
        if p:
            q=self.quotes.get(symbol)
            if q and -2000<=now_ms()-q.ts<=8000:value+=(1 if p['side']=='LONG' else -1)*(q.exit(p['side'])-p['entry'])*p['qty']
            else:return number(st.get('equity',value))
        return value
    def can_enter(self,symbol):
        s=self.state['legs'][symbol]
        return bool(self.authorized() and not self.state.get('halt') and not self.state.get('entry_blocks')
                    and not s.get('position') and not s.get('halt'))
    def entry(self,sig,now):
        if self.store.seen(sig['key']):return False
        if not self.can_enter(sig['symbol']):
            self.store.signal(sig['key'],{'decision':'PAPER_BLOCKED','signal':sig});return False
        symbol=sig['symbol'];st=self.state['legs'][symbol]
        try:
            blocks=risk.blocks(st,max(.000001,self.leg_equity(symbol)),self.c,now)
            if blocks:raise SafetyError(','.join(blocks))
            q=self.api.quote(symbol);self.quotes[symbol]=q
            sized=strategy.size(sig,q,self.instruments[symbol],self.c,self.leg_equity(symbol),now_ms())
            account_equity=self.state['cash_reserve']+sum(self.leg_equity(s) for s in SYMBOLS)
            held_margin=sum(leg['position']['margin'] for leg in self.state['legs'].values() if leg.get('position'))
            required=sized['margin']+2*sized['notional']*sized['fee_rate']+self.c['margin_reserve_usdt']
            if account_equity-held_margin<required:raise SafetyError('INSUFFICIENT_FREE_MARGIN_WITH_RESERVE')
            if not self.authorized():raise SafetyError('PAUSED_DURING_PAPER_PREFLIGHT')
        except SafetyError as exc:
            self.save();self.store.signal(sig['key'],{'decision':'PAPER_REJECTED','reason':str(exc),'signal':sig});raise
        # An adverse IOC-bound price is an explicit conservative simulated fill.
        # It is not a claim that a real IOC would fill this quantity.
        fill=sized['limit'];inst=self.instruments[symbol];fee=sized['qty']*fill*sized['fee_rate']
        p={'id':'PAPER_'+uuid.uuid4().hex,'symbol':symbol,'side':sig['side'],'qty':sized['qty'],
           'original_qty':sized['qty'],'entry':fill,'opened':now_ms(),'stop':sized['stop'],'initial_stop':sized['stop'],
           'price_step':inst.price_step,'qty_step':inst.qty_step,'entry_fee':fee,'fee_rate':sized['fee_rate'],
           'risk_budget':sized['risk_budget'],'initial_risk_usdt':sized['estimated_risk'],'margin':sized['margin'],
           'notional':sized['notional'],'config':copy.deepcopy(self.c),'signal':copy.deepcopy(sig),
           'exit_bar':sig['bar_end'],'last_sample':q.ts,'sample_gap':False,'protection_ids':[],
           'accounting':'PAPER_BEFORE_FUNDING_NO_LIQUIDATION_MODEL'}
        p['target']=fill+(1 if p['side']=='LONG' else -1)*2*abs(fill-p['stop']);p['expires']=p['opened']+3600000
        st['cash']-=fee;st['position']=p
        # A crash cannot commit the position but forget that its signal was used.
        with self.store.db:
            self.store.db.execute('INSERT INTO signals VALUES(?,?,?)',(sig['key'],now_ms(),json.dumps({'decision':'PAPER_FILLED','signal':sig},allow_nan=False)))
            self.store.db.execute('INSERT OR REPLACE INTO state VALUES(?,?)',('engine',json.dumps(self.state,allow_nan=False)))
            self.store.db.execute('INSERT INTO events(ts,kind,json) VALUES(?,?,?)',(now_ms(),'OPEN',json.dumps({'symbol':symbol,'side':p['side'],'entry':fill,'qty':p['qty'],'stop':p['stop'],'target':p['target'],'probability':sig['frame']['probability'],'event_type':(sig['frame'].get('event') or {}).get('event_type'),'mode':'PAPER','initial_risk_usdt':p['initial_risk_usdt']},allow_nan=False)))
        self.log('OPEN',{'symbol':symbol,'side':p['side'],'mode':'PAPER','entry':fill,'qty':p['qty'],'stop':p['stop'],'target':p['target'],'probability':sig['frame']['probability'],'funding':'UNMODELED'});return True
    def tick(self,q,f,now):
        q.validate(now);self.quotes[q.symbol]=q;st=self.state['legs'][q.symbol];p=st.get('position')
        if not p:return
        if q.ts<=p.get('last_sample',0):return
        if q.ts-p.get('last_sample',q.ts)>p['config']['max_tick_gap_seconds']*1000:p['sample_gap']=True
        p['last_sample']=q.ts
        reason=p.get('force_exit') or strategy.manage(p,q,f,p['config'],now)
        if reason:self.close(p,q,reason,now)
        else:self.save()
    def close(self,p,q,reason,now):
        sign=1 if p['side']=='LONG' else -1
        fill=q.exit(p['side'])*(1-sign*p['config']['exit_slippage_bps']/10_000)
        fee=p['qty']*fill*p['fee_rate'];gross=sign*(fill-p['entry'])*p['qty'];net=gross-p['entry_fee']-fee
        st=self.state['legs'][p['symbol']];st['cash']+=gross-fee;st['position']=None
        risk.closed(st,net,p['config'],now);risk.closed(self.state['portfolio_guard'],net,p['config'],now)
        self.state['portfolio_guard']['cooldown_until']=0
        trade={'id':p['id'],'symbol':p['symbol'],'side':p['side'],'opened':p['opened'],'closed':now,'entry':p['entry'],
               'exit':fill,'qty':p['qty'],'gross_usdt':gross,'fees_usdt':p['entry_fee']+fee,'funding_usdt':None,
               'net_usdt':None,'net_before_funding_usdt':net,'reason':reason,'sample_gap':p['sample_gap'],
               'accounting':p['accounting'],'config':p['config'],'signal':p['signal']}
        self.state['equity']=sum(self.leg_equity(s) for s in SYMBOLS)
        self.store.finish(trade,self.state);self.event('CLOSE',{'symbol':p['symbol'],'mode':'PAPER','net_usdt':None,'net_before_funding_usdt':net,'reason':reason,'accounting':p['accounting']})
    def poll(self,now):
        eq=sum(self.leg_equity(s) for s in SYMBOLS)
        if eq<=0:self.halt('PAPER_EQUITY_EXHAUSTED')
        blocks=risk.blocks(self.state['portfolio_guard'],max(eq,.000001),self.c,now)
        self.state['equity']=eq;self.state['account_equity']=eq+self.state['cash_reserve'];self.state['entry_blocks']=blocks
        for s in SYMBOLS:
            st=self.state['legs'][s];leq=self.leg_equity(s)
            legblocks=risk.blocks(st,max(leq,.000001),self.c,now)
            if leq<=0:st['halt']='PAPER_LEG_EQUITY_EXHAUSTED'
            if st.get('position') and any(x in ('MAX_DRAWDOWN','DAILY_LOSS_LIMIT','WEEKLY_LOSS_LIMIT') for x in blocks+legblocks):
                st['position']['force_exit']='ACCOUNT_LOSS_LIMIT'
        self.save()
