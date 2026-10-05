"""Read-only account inventory and strict ownership/protection checks."""
from .core import *
from .api import rows
import time

NON_USDT_DUST_LIMIT_USD = 1.0

def margin_breakdown(assets):
    """Conservative free collateral for this USDT-only crossed release.

    UTA v3 `/account/assets` documents effEquity/imr in USD and each coin's
    equity/available in that coin; usdValue is its USD valuation. Do not mistake
    total usdtEquity or a spot coin balance for unused futures margin.
    Source: https://www.bitget.com/docs/catalog/account/assets-balance
    """
    if not isinstance(assets,dict) or not isinstance(assets.get('assets'),list):
        raise DataError('AVAILABLE_MARGIN_SCHEMA_MISSING')
    effective=number(assets.get('effEquity'));initial=number(assets.get('imr'))
    if initial<0:raise DataError('NEGATIVE_INITIAL_MARGIN')
    coins={};dust_usd=0.;dust_coins=[]
    for item in assets['assets']:
        if not isinstance(item,dict) or not isinstance(item.get('coin'),str):
            raise DataError('ASSET_COIN_MISSING')
        coin=item['coin']
        if coin in coins:raise DataError('DUPLICATE_ASSET_COIN')
        values={k:number(item.get(k)) for k in ('equity','usdValue','balance','available','debt','locked')}
        if values['debt']<0 or values['locked']<0:raise DataError('INVALID_ASSET_LIABILITY')
        if values['debt']>0:raise SafetyError('ACCOUNT_BORROWING_PRESENT')
        if coin!='USDT' and any(values[k]!=0 for k in ('equity','usdValue','balance','available','locked')):
            # Small, fully valued, unborrowed and unlocked leftovers may stay
            # in the account. They must never fund a futures entry. Subtract
            # their FULL USD value even if the exchange applies a haircut or
            # does not enable them as collateral; that is conservative.
            if (any(values[k]<0 for k in ('equity','usdValue','balance','available'))
                    or values['locked']!=0 or values['usdValue']<=0
                    or values['equity']<=0 or values['balance']<=0):
                raise SafetyError('NON_USDT_COLLATERAL_UNSUPPORTED')
            dust_usd+=values['usdValue'];dust_coins.append(coin)
            if dust_usd>NON_USDT_DUST_LIMIT_USD:
                raise SafetyError('NON_USDT_COLLATERAL_UNSUPPORTED')
        coins[coin]=values
    if 'USDT' not in coins:raise DataError('USDT_ASSET_MISSING')
    coin=coins['USDT']
    available=0.
    if coin['equity']>0 and coin['usdValue']>0:
        usdt_per_usd=coin['equity']/coin['usdValue']
        available=max(0.,min(coin['available'],(effective-initial-dust_usd)*usdt_per_usd))
    return {'available_usdt':available,'excluded_dust_usd':dust_usd,
            'dust_coins':sorted(dust_coins),'dust_limit_usd':NON_USDT_DUST_LIMIT_USD}

def available_usdt(assets):
    return margin_breakdown(assets)['available_usdt']

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
    started=time.monotonic()
    def read(fn,*args):
        if time.monotonic()-started>20:raise DataError('ACCOUNT_INVENTORY_TIMEOUT')
        result=fn(*args)
        if time.monotonic()-started>20:raise DataError('ACCOUNT_INVENTORY_TIMEOUT')
        return result
    positions=[];orders=[];strategies=[]
    for cat in ('USDT-FUTURES','USDC-FUTURES','COIN-FUTURES'):
        positions+=read(api.positions,cat);orders+=read(api.open_orders,cat)
        for kind in ('tpsl','trigger','oco','trailing_stop','iceberg','twap'):
            strategies+=read(api.strategies,cat,kind)
    for cat in ('SPOT','MARGIN'):
        orders+=read(api.open_orders,cat)
        for kind in ('trigger','oco','iceberg','twap'):strategies+=read(api.strategies,cat,kind)
    assets=read(api.assets);settings=read(api.settings)
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
    """Verify the exchange's original structural stop, not the local trail.

    No documented post-fill TPSL modification is used in this release. Once
    discovered after the entry, stop IDs are bound to that position permanently.
    A new same-price manual order must not silently replace a missing owned stop.
    """
    covered={};full_ids=set()
    owned=set(map(str,p.get('bound_protection_ids',[])))
    for r in orders:
        if r.get('symbol')!=p['symbol'] or r.get('category',CAT)!=CAT:continue
        if str(r.get('posSide','')).lower()!=p['side'].lower():continue
        if str(r.get('status','')).lower()!='pending':continue
        if r.get('slTriggerBy')!='mark' or r.get('slOrderType')!='market':continue
        full=str(r.get('tpslMode','')).lower() in ('full','full_position') or r.get('planType')=='pos_loss'
        if r.get('stopLoss') in (None,''):continue
        stop=number(r['stopLoss'],True)
        if abs(stop-p['initial_stop'])>p['price_step']/2:continue
        if r.get('orderId'):
            oid=str(r['orderId'])
            if owned and oid not in owned:continue
            if full:full_ids.add(oid)
            else:covered[oid]=number(r.get('qty',0))
    if full_ids:return sorted(full_ids)
    return sorted(covered) if sum(covered.values())>=p['qty']-p['qty_step']/2 else []

def position_history(api,symbol,now):
    # History retention is 90 days, but each request may span at most 30 days.
    # Cover the same 89-day lookback in bounded windows and retain pagination.
    start=max(0,now-89*DAY);unique={}
    while start<=now:
        end=min(now,start+30*DAY-1)
        batch=api.pages('/api/v3/position/history-position',{'category':CAT,'symbol':symbol,
                        'startTime':str(start),'endTime':str(end)})
        for row in batch:
            if not isinstance(row,dict) or not row.get('positionId'):
                raise DataError('history position identity missing')
            key=str(row['positionId'])
            if key in unique and unique[key]!=row:raise DataError('conflicting history position record')
            unique[key]=row
        start=end+1
    return list(unique.values())

def history_match(api,p,now):
    # Match opening time, price and full quantity, not just the symbol.
    data=position_history(api,p['symbol'],now)
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
