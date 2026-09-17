"""Read-only account inventory and strict ownership/protection checks."""
from .core import *
from .api import rows

def identity(api,store):
    info=api.get('/api/v3/account/info',private=True)
    if not isinstance(info,dict) or not info.get('userId'):raise DataError('account identity missing')
    uid=str(info['userId']);bound=store.get('account_uid')
    if bound and str(bound)!=uid:raise SafetyError('ACCOUNT_CHANGED: refusing to use another account state')
    perms={str(p).lower() for p in info.get('permissions',[])}
    if any('withdraw' in p for p in perms):raise SafetyError('Use an API key without withdrawal permission')
    if not {'uta_trade','uta_mgt'}<=perms:raise SafetyError('UTA trade + management permissions required')
    if not bound:store.set('account_uid',uid)
    return uid

def correct_settings(settings,symbol):
    if not isinstance(settings,dict) or settings.get('accountMode')!='unified':return False
    if settings.get('holdMode') not in ('one_way_mode','hedge_mode'):return False
    found=[r for r in settings.get('symbolConfigList',[]) if r.get('category')==CAT and r.get('symbol')==symbol and r.get('marginMode')=='crossed']
    return bool(found) and all(number(r.get('leverage'))==5 for r in found)

def inventory(api):
    positions=[];orders=[];strategies=[]
    for cat in ('USDT-FUTURES','USDC-FUTURES','COIN-FUTURES'):
        positions+=api.positions(cat);orders+=api.open_orders(cat)
        for kind in ('tpsl','trigger','oco','trailing_stop','iceberg','twap'):
            strategies+=api.strategies(cat,kind)
    for cat in ('SPOT','MARGIN'):
        orders+=api.open_orders(cat)
        for kind in ('trigger','oco','iceberg','twap'):strategies+=api.strategies(cat,kind)
    assets=api.assets();settings=api.settings()
    return {'positions':positions,'orders':orders,'strategies':strategies,'assets':assets,
            'equity':number(assets.get('usdtEquity'),True),'settings':settings}

def require_flat(snap):
    if snap['positions'] or snap['orders'] or snap['strategies']:
        raise SafetyError('ACCOUNT_NOT_FLAT: positions/open orders/strategies exist; no cancellation or adoption')
    # Borrowing and other collateral make the equity-risk interpretation different.
    a=snap['assets']
    for key in ('debt','totalDebt','usdtDebt'):
        if key in a and number(a[key])>0:raise SafetyError('ACCOUNT_BORROWING_PRESENT')

def matching(positions,p):
    found=[r for r in positions if r.get('symbol')==p['symbol'] and
           r.get('category',CAT)==CAT and str(r.get('posSide','')).lower()==p['side'].lower()]
    if len(found)>1:raise SafetyError('AMBIGUOUS_POSITION')
    return found[0] if found else None

def protection(orders,p):
    covered={};full_ids=set()
    for r in orders:
        if r.get('symbol')!=p['symbol'] or r.get('category',CAT)!=CAT:continue
        if str(r.get('posSide','')).lower()!=p['side'].lower():continue
        if str(r.get('status','')).lower() in ('cancelled','canceled','failed','triggered'):continue
        if r.get('slTriggerBy')!='mark' or r.get('slOrderType','market')!='market':continue
        full=str(r.get('tpslMode','')).lower() in ('full','full_position') or r.get('planType')=='pos_loss'
        if r.get('stopLoss') in (None,''):continue
        stop=number(r['stopLoss'],True)
        if abs(stop-p['initial_stop'])>p['price_step']/2:continue
        if r.get('orderId'):
            oid=str(r['orderId'])
            if full:full_ids.add(oid)
            else:covered[oid]=number(r.get('qty',0))
    if full_ids:return sorted(full_ids)
    return sorted(covered) if sum(covered.values())>=p['qty']-p['qty_step']/2 else []

def history_match(api,p,now):
    # Query the last 90 days. Match the original opening time and quantity, not
    # just the symbol. Account is exclusive while this bot has exposure.
    data=api.pages('/api/v3/position/history-position',{'category':CAT,'symbol':p['symbol'],
                   'startTime':str(max(0,now-89*DAY)),'endTime':str(now)})
    candidates=[]
    for r in data:
        if r.get('symbol')!=p['symbol'] or str(r.get('posSide','')).lower()!=p['side'].lower():continue
        if abs(int(r.get('createdTime',0))-p['opened'])>10_000:continue
        if abs(number(r.get('openTotalPos'))-p['original_qty'])>p['qty_step']/2:continue
        if abs(number(r.get('closeTotalPos'))-p['original_qty'])>p['qty_step']/2:continue
        if abs(number(r.get('openPriceAvg'))-p['entry'])>max(p['price_step']*2,p['entry']*1e-6):continue
        candidates.append(r)
    if len(candidates)!=1:return None
    r=candidates[0]
    # On UTA history these fees are signed cashflows (negative for expense).
    gross=number(r.get('cumRealisedPnl'));funding=number(r.get('totalFunding'))
    entry_fee=number(r.get('openFeeTotal'));exit_fee=number(r.get('closeFeeTotal'));net=number(r.get('netProfit'))
    if abs(net-(gross+funding+entry_fee+exit_fee))>max(.02,abs(net)*1e-6):
        raise SafetyError('HISTORY_ACCOUNTING_MISMATCH: net != realised + funding + signed fees')
    exit_price=number(r.get('closePriceAvg'),True)
    check=(exit_price-p['entry'])*(1 if p['side']=='LONG' else -1)*p['original_qty']
    if abs(gross-check)>max(.05,p['entry']*p['original_qty']*1e-5):
        raise SafetyError('HISTORY_PRICE_PNL_MISMATCH')
    if not r.get('positionId'):raise DataError('history position identity missing')
    return {'position_id':str(r['positionId']),'closed':int(r['updatedTime']),'exit':exit_price,
            'gross_usdt':gross,'fee_cashflow_usdt':entry_fee+exit_fee,'funding_usdt':funding,
            'net_usdt':net,'accounting':'EXCHANGE_POSITION_HISTORY_VERIFIED'}
