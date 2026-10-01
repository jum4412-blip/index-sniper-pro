"""Small UTA v3 adapter. Credentials never logged; order writes never retried."""
import base64,hashlib,hmac,json,os,threading,time,urllib.parse,urllib.request,urllib.error
from pathlib import Path
from .core import *

PUBLIC={'/api/v3/market/instruments','/api/v3/market/candles','/api/v3/market/history-candles','/api/v3/market/tickers','/api/v3/market/history-fund-rate'}
WRITES={'/api/v3/trade/place-order','/api/v3/trade/cancel-order','/api/v3/account/set-leverage'}

def env_values(path):
    values={}
    if Path(path).is_file():
        for line in Path(path).read_text().splitlines():
            line=line.strip()
            if not line or line.startswith('#'):continue
            if line.startswith('export '):line=line[7:]
            if '=' not in line:continue
            k,v=line.split('=',1);k=k.strip();v=v.strip()
            if not k.replace('_','').isalnum():continue
            if len(v)>=2 and v[0]==v[-1] and v[0] in ('"',"'"):v=v[1:-1]
            else:v=v.split(' #',1)[0].rstrip()
            values[k]=v
    values.update(os.environ)
    return values

def credentials(path):
    e=env_values(path)
    c={'key':e.get('BITGET_API_KEY',''),'secret':e.get('BITGET_SECRET_KEY') or e.get('BITGET_API_SECRET',''),
       'passphrase':e.get('BITGET_PASSPHRASE') or e.get('BITGET_API_PASSPHRASE','')}
    if not all(c.values()):raise SafetyError("Bitget credentials missing in the local .env; do not upload keys")
    return c

def rows(data,context='API'):
    result=data if isinstance(data,list) else data.get('list') if isinstance(data,dict) else None
    if not isinstance(result,list):
        # Report field names/types only; no balances, identifiers, or raw API body.
        import re
        keys=','.join(sorted(k for k in data if isinstance(k,str) and re.fullmatch(r'[A-Za-z0-9_]{1,40}',k))[:12]) if isinstance(data,dict) else '-'
        raise DataError(context+": API list missing; refusing to treat it as empty; data_type="+type(data).__name__+"; keys="+keys)
    if any(not isinstance(row,dict) for row in result):raise DataError(context+': invalid API list row')
    return result

def position_rows(data,context='/api/v3/position/current-position'):
    # Observed on this UTA account for all three categories and BTC/ETH queries:
    # successful 00000 responses encode an empty current-position list as null.
    # Only the exact observed shape on this endpoint is normalized. Missing data,
    # missing list, error-shaped objects and other endpoints remain fail-closed.
    if isinstance(data,dict) and set(data)=={'list'} and data['list'] is None:
        return []
    return rows(data,context)

def history_rows(data,path,context):
    # EC2 observation 2026-09-21: an empty 30-day history window returns
    # successful data={list:null,cursor:<empty>}. Scope this exception to this
    # endpoint and exact fields. Never normalize failed/malformed order reads.
    if (path=='/api/v3/position/history-position' and isinstance(data,dict)
            and set(data)=={'list','cursor'} and data['list'] is None
            and (data['cursor'] is None or data['cursor']=='')):
        return []
    return rows(data,context)

class Rest:
    def __init__(self,creds=None,write=False,opener=None):
        self.creds=creds;self.write=write;self.opener=opener or urllib.request.urlopen
        self.lock=threading.Lock();self.next_request=0.;self.funding_cache={}
    def request(self,method,path,params=None,body=None,private=False):
        if method not in ('GET','POST') or not path.startswith('/api/v3/'):raise SafetyError("unsupported request")
        if private and not self.creds:raise SafetyError("private API unavailable in paper mode")
        if not private and (method!='GET' or path not in PUBLIC):raise SafetyError("public endpoint not allowed")
        if method=='POST' and (not self.write or path not in WRITES):raise SafetyError("API write not allowed")
        query=urllib.parse.urlencode(sorted((params or {}).items()))
        target=path+('?' + query if query else '')
        payload=json.dumps(body,separators=(',',':')) if body is not None else ''
        for attempt in range(2 if method=='GET' else 1):
            with self.lock:
                gap=self.next_request-time.monotonic()
                if gap>0:time.sleep(gap)
                self.next_request=time.monotonic()+.16
            hdr={'Content-Type':'application/json','User-Agent':'RangeBasic/1.0.0-rc1'}
            if private:
                stamp=str(now_ms());c=self.creds
                signature=base64.b64encode(hmac.new(c['secret'].encode(),(stamp+method+target+payload).encode(),hashlib.sha256).digest()).decode()
                hdr.update({'ACCESS-KEY':c['key'],'ACCESS-PASSPHRASE':c['passphrase'],'ACCESS-TIMESTAMP':stamp,'ACCESS-SIGN':signature})
            req=urllib.request.Request('https://api.bitget.com'+target,data=payload.encode() if payload else None,headers=hdr,method=method)
            try:
                with self.opener(req,timeout=5) as response:result=json.loads(response.read())
                if not isinstance(result,dict) or 'code' not in result:
                    if method=='POST':raise UnknownOrder("unparseable order response; reconcile, do not resend")
                    raise DataError("invalid API envelope")
                code=str(result['code'])
                if code!='00000':
                    if method=='POST' and code in {'40010','40725','45001'}:raise UnknownOrder("ambiguous exchange response "+code)
                    raise Rejected(path+": exchange code "+code)
                if 'data' not in result:
                    if method=='POST':raise UnknownOrder("missing order data; reconcile")
                    raise DataError("API data missing")
                return result['data']
            except (urllib.error.URLError,TimeoutError,OSError,ValueError) as exc:
                if method=='POST':raise UnknownOrder("transport outcome unknown; reconcile by clientOid") from None
                if attempt==1:raise DataError(path+": read failed") from None
                time.sleep(.3)
        raise DataError("read failed")
    def get(self,path,params=None,private=False):return self.request('GET',path,params=params,private=private)
    def post(self,path,body):return self.request('POST',path,body=body,private=True)
    def pages(self,path,params):
        out=[];cursor=None;seen=set()
        for _ in range(100):
            d=self.get(path,{**params,'limit':'100',**({'cursor':cursor} if cursor else {})},True)
            context=path+' category='+str(params.get('category',''))+' type='+str(params.get('type',''))
            batch=history_rows(d,path,context);out+=batch
            if len(batch)<100:return out
            if not isinstance(d,dict) or not d.get('cursor') or d['cursor'] in seen:
                raise DataError("pagination completeness unknown")
            cursor=d['cursor'];seen.add(cursor)
        raise DataError("pagination limit reached")
    def instrument(self,symbol):
        r=rows(self.get('/api/v3/market/instruments',{'category':CAT,'symbol':symbol}))
        found=[x for x in r if x.get('symbol')==symbol]
        if len(found)!=1:raise DataError("instrument not found")
        return Instrument.parse(found[0])
    def settled_funding(self,symbol,now=None):
        """Most recent actually settled funding; ticker rate is not a substitute."""
        now=now if now is not None else now_ms()
        cached=self.funding_cache.get(symbol)
        if cached and now//300000==cached['read_at']//300000 and 0<=now-cached['read_at']<300000 and 0<=now-cached['timestamp']<=16*3600000:return dict(cached)
        data=self.get('/api/v3/market/history-fund-rate',{'category':CAT,'symbol':symbol,'limit':'20','cursor':'1'})
        if not isinstance(data,dict) or not isinstance(data.get('resultList'),list):raise DataError('SETTLED_FUNDING_HISTORY_MISSING')
        valid=[]
        for row in data['resultList']:
            if not isinstance(row,dict):raise DataError('SETTLED_FUNDING_ROW_INVALID')
            if row.get('symbol')!=symbol:continue
            stamp=number(row.get('fundingRateTimestamp'),True)
            if stamp!=int(stamp):raise DataError('SETTLED_FUNDING_TIMESTAMP_INVALID')
            rate=number(row.get('fundingRate'))
            if stamp<=now:valid.append((int(stamp),rate))
        if not valid:raise DataError('SETTLED_FUNDING_HISTORY_EMPTY')
        stamp,rate=max(valid,key=lambda item:item[0])
        matches={r for ts,r in valid if ts==stamp}
        if len(matches)!=1:raise DataError('SETTLED_FUNDING_CONFLICT')
        if not 0<=now-stamp<=16*3600000:raise DataError('SETTLED_FUNDING_STALE')
        result={'symbol':symbol,'timestamp':stamp,'rate':rate,'read_at':now,'source':'Bitget UTA v3 history-fund-rate settled observation'}
        self.funding_cache[symbol]=result;return dict(result)
    def quote(self,symbol):
        return self.quotes((symbol,))[symbol]
    def quotes(self,symbols=SYMBOLS):
        """One public snapshot for both independent symbol managers."""
        wanted=tuple(symbols)
        if not wanted or len(set(wanted))!=len(wanted) or any(s not in SYMBOLS for s in wanted):
            raise DataError('unsupported ticker symbols')
        params={'category':CAT}
        if len(wanted)==1:params['symbol']=wanted[0]
        r=rows(self.get('/api/v3/market/tickers',params))
        result={}
        for symbol in wanted:
            found=[x for x in r if x.get('symbol')==symbol]
            if len(found)!=1:raise DataError("ticker not found or duplicated")
            result[symbol]=self._quote(found[0],symbol)
        return result
    @staticmethod
    def _quote(d,symbol):
        # UTA REST ticker uses ts. Never replace a missing timestamp with local now.
        q=Quote(symbol,int(d['ts']),*[number(d[k]) for k in ('lastPrice','bid1Price','ask1Price','markPrice','indexPrice','fundingRate')])
        q.validate(now_ms());return q
    def candles(self,symbol,interval,count):
        if symbol not in SYMBOLS or interval not in ('1m','3m','5m','15m','30m','1H','4H','6H','12H','1D'):
            raise DataError('unsupported candle request')
        if not isinstance(count,int) or isinstance(count,bool) or not 1<=count<=2000:
            raise DataError('invalid candle count')
        out={};end=None
        for _ in range(12):
            path='/api/v3/market/candles' if end is None else '/api/v3/market/history-candles'
            # Current candles support 1,000 rows; history remains conservatively
            # paged at 100. The strategy, not this transport, excludes forming bars.
            limit=min(count,1000 if end is None else 100)
            d=self.get(path,{'category':CAT,'symbol':symbol,'interval':interval,'type':'market','limit':str(limit),**({'endTime':str(end)} if end else {})})
            if not isinstance(d,list):raise DataError("candle response missing")
            b=[Bar.parse(r) for r in d]
            if not b:break
            oldest=min(x.ts for x in b)
            if end is not None and oldest>=end:raise DataError("candle pagination stalled")
            for bar in b:
                if bar.ts in out and out[bar.ts]!=bar:raise DataError('conflicting candle pages')
                out[bar.ts]=bar
            if len(out)>=count:break
            # Bitget floors the end boundary. Subtracting 1 ms can drop the
            # immediately preceding bar at each historical page boundary.
            end=oldest
        if len(out)<count:raise DataError('candle history incomplete')
        return sorted(out.values(),key=lambda b:b.ts)[-count:]
    def positions(self,category=CAT):
        path='/api/v3/position/current-position'
        return [r for r in position_rows(self.get(path,{'category':category},True),path+' category='+category) if abs(number(r.get('total')))>0]
    def assets(self):
        d=self.get('/api/v3/account/assets',private=True)
        if not isinstance(d,dict):raise DataError("account assets missing")
        number(d.get('usdtEquity'),True);return d
    def settings(self):return self.get('/api/v3/account/settings',private=True)
    def open_orders(self,category=CAT):return self.pages('/api/v3/trade/unfilled-orders',{'category':category})
    def strategies(self,category=CAT,kind='tpsl'):
        return self.pages('/api/v3/trade/unfilled-strategy-orders',{'category':category,'type':kind})
    def order(self,oid='',cid=''):
        return self.get('/api/v3/trade/order-info',{'orderId':oid} if oid else {'clientOid':cid},True)
    def fills(self,oid):return self.pages('/api/v3/trade/fills',{'category':CAT,'orderId':oid})

def fill_summary(fills):
    unique={}
    for f in fills:
        if not f.get('execId'):raise DataError("execution id missing")
        if f['execId'] in unique and unique[f['execId']]!=f:raise DataError("conflicting execution id")
        unique[f['execId']]=f
    fs=list(unique.values())
    if not fs:return None
    qty=sum(number(f['execQty'],True) for f in fs)
    price=sum(number(f['execQty'],True)*number(f['execPrice'],True) for f in fs)/qty
    fees=0.;pnl=0.
    for f in fs:
        if f.get('execPnl') in (None,''):pnl=None
        elif pnl is not None:pnl+=number(f['execPnl'])
        details=f.get('feeDetail')
        if not isinstance(details,list):fees=None
        elif fees is not None:
            for d in details:
                if d.get('feeCoin')!='USDT':fees=None;break
                fees+=number(d.get('fee'))
    return {'qty':qty,'price':price,'fees':fees,'pnl':pnl,'fills':fs,
            'first':min(int(f['createdTime']) for f in fs),'last':max(int(f['createdTime']) for f in fs)}
