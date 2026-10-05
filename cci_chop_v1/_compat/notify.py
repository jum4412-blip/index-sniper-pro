"""One Telegram destination, durable outbox, asynchronous bounded network calls.

Telegram has no idempotency key. Delivery is at least once: a process crash after
Telegram accepts a message but before the SQLite acknowledgement can duplicate it.
Trading/order reconciliation never waits for Telegram delivery.
"""
import hashlib,json,sqlite3,threading,time,urllib.request,urllib.error
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from .api import env_values
from .core import SYMBOLS,SafetyError,now_ms

EVENTS={'OPEN','CLOSE','HALT','STOPPED','HEARTBEAT','ORDER_UNKNOWN','ORDER_REJECTED','ORDER_UNFILLED','URGENT_EXIT_REJECTED','BLOCKED_OR_UNAVAILABLE','CANDLES_UNAVAILABLE','QUOTE_UNAVAILABLE','LOCAL_TRAIL_STALE'}
DURABLE_EVENTS={'OPEN','CLOSE','HALT','ORDER_UNKNOWN','ORDER_REJECTED','ORDER_UNFILLED','URGENT_EXIT_REJECTED','LOCAL_TRAIL_STALE'}
ROUTINE_EVENTS={'ORDER_REJECTED','ORDER_UNFILLED','BLOCKED_OR_UNAVAILABLE','CANDLES_UNAVAILABLE','QUOTE_UNAVAILABLE'}
SIX_HOURS_MS=6*60*60*1000
KST=ZoneInfo('Asia/Seoul')
REASONS={
    'NO_COMPLETED_PRICE_VOLUME_EVENT':'완료된 5분봉 진입 패턴 없음',
    'INSUFFICIENT_SUPPORT':'해당 조건의 과거 표본 부족',
    'WAIT_SAMPLE_SUPPORT':'해당 조건의 과거 표본 부족',
    'UNCERTAIN_OR_NEGATIVE_NET_EDGE':'비용 포함 기대값 기준 미달',
    'WAIT_POSITIVE_NET_EDGE':'비용 포함 기대값 기준 미달',
    'MODEL_LABEL_NOTIONAL_MISMATCH':'과거 모형의 주문 규모와 현재 실매매 설정이 일치하지 않음',
    'UNKNOWN_STATE':'학습되지 않은 가격·거래량 상태',
    'STRUCTURAL_STOP_OUTSIDE_FROZEN_RANGE':'손절 거리 기준 미달',
    'WAIT_ONE_MINUTE_DELAY':'완료 봉 이후 1분 대기',
    'QUALIFIED_EVENT_WINDOW':'진입 후보 확인 중',
    'EVENT_WINDOW_EXPIRED':'진입 가능 시간 만료',
    'CANDLE_WARMUP_PENDING':'5분봉 데이터 수집 중',
    'STALE_FRAME':'최신 5분봉 확인 지연',
    'MAX_DRAWDOWN':'최대 낙폭 제한',
    'DAILY_LOSS_LIMIT':'일일 손실 제한',
    'WEEKLY_LOSS_LIMIT':'주간 손실 제한',
    'STOP':'손절', 'TARGET':'목표가 도달', 'MAX_HOLD':'최대 보유시간',
    'ACCOUNT_LOSS_LIMIT':'계좌 손실 제한', 'EXCHANGE_OR_EXTERNAL_CLOSE':'거래소 또는 외부 청산',
}

def korean_reason(value):
    if value is None:return '확인 중'
    raw=str(value)
    if raw.startswith('PROBABILITY_MODEL_UNAVAILABLE'):return '확률 모델 확인 필요'
    # An exception reason may contain exchange response text. Keep unexpected
    # detail in the local log instead of forwarding it to a chat.
    return REASONS.get(raw,'원인 확인 필요 (서버 로그 참조)')

def kst(ms):
    try:return datetime.fromtimestamp(int(ms)/1000,KST).strftime('%Y-%m-%d %H:%M KST')
    except (TypeError,ValueError,OverflowError,OSError):return '시각 확인 중'

def amount(value,places=2):
    try:return f'{float(value):,.{places}f}'
    except (TypeError,ValueError,OverflowError):return '확인 중'

def credentials(env):
    values=env_values(env)
    token=values.get('TELEGRAM_BOT_TOKEN') or values.get('TG_BOT_TOKEN') or values.get('TELEGRAM_TOKEN','')
    chat=values.get('TELEGRAM_CHAT_ID') or values.get('TG_CHAT_ID') or values.get('TELEGRAM_CHAT','')
    if not token or not chat:raise SafetyError('TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing in local .env')
    if any(ch.isspace() for ch in token+chat):raise SafetyError('invalid Telegram configuration')
    return {'token':token,'chat':chat}

def render(kind,data,mode='live',event_ms=None):
    mode_text='실매매' if mode=='live' else '모의매매'
    titles={'OPEN':'진입 체결','CLOSE':'청산 확인','HALT':'매매 중단','STOPPED':'관리 프로세스 종료',
            'HEARTBEAT':'BTC·ETH 상황 요약','ORDER_UNKNOWN':'주문 확인 필요',
            'ORDER_REJECTED':'주문 거절','ORDER_UNFILLED':'주문 미체결',
            'URGENT_EXIT_REJECTED':'긴급 청산 확인 필요','BLOCKED_OR_UNAVAILABLE':'계좌·연결 확인 필요',
            'CANDLES_UNAVAILABLE':'5분봉 확인 지연','QUOTE_UNAVAILABLE':'시세 확인 지연',
            'LOCAL_TRAIL_STALE':'청산 관리자 확인 필요'}
    lines=[f'[BTC·ETH 확률봇 {mode_text}] {titles.get(kind,"운영 알림")}',
           '시각: '+kst(event_ms if event_ms is not None else data.get('at',now_ms()))]
    if kind=='OPEN':
        side='매수' if data.get('side')=='LONG' else '매도' if data.get('side')=='SHORT' else '방향 확인 중'
        lines.extend([f"{data.get('symbol')} {side} / 체결수량 {data.get('qty')}",
                      f"진입가 {data.get('entry')} / 손절가 {data.get('stop')} / 목표가 {data.get('target')}",
                      f"비용 포함 계획손실 {amount(data.get('initial_risk_usdt'))} USDT (급변 시 초과 가능)"])
        event=data.get('event_type')
        if event:lines.append('진입 근거: '+{'sweep_reclaim':'이전 고점·저점 이탈 후 복귀','breakout_acceptance':'돌파 후 가격 유지'}.get(event,'가격·거래량 사건'))
        probability=data.get('probability',{})
        if probability:lines.append(f"훈련 표본 그룹 {probability.get('n_group')} / 상태 {probability.get('n_state')} / 순이익 확률 추정 {probability.get('p_mean')} / 하한 {probability.get('p05')} / 손익분기 {probability.get('break_even')} / 기대순R 하한 {probability.get('lower95')}")
    elif kind=='CLOSE':
        lines.extend([str(data.get('symbol','')),f"정산 순손익 {amount(data.get('net_usdt'))} USDT / 펀딩 {amount(data.get('funding_usdt'))} USDT",'청산 사유: '+korean_reason(data.get('reason'))])
        if data.get('net_usdt') is None:lines.append('모의매매는 펀딩을 포함한 확정 순손익을 계산하지 않습니다.')
    elif kind=='HEARTBEAT':
        data={**data,'positions':data.get('positions',{s:v.get('position') for s,v in data.get('legs',{}).items()}),'halt':data.get('halt',data.get('risk_halt')),'probability':data.get('probability',data.get('trends',{}))}
        allocation=data.get('allocation_each')
        notional=data.get('target_notional_each')
        sizing=(f'각 {amount(allocation,0)} USDT / 교차 5배 / 목표 포지션 각 {amount(notional,0)} USDT'
                if allocation is not None and notional is not None else '진입 규모: 실행 상태에서 확인 필요')
        lines.extend([sizing,
                      '계좌 평가액: '+(amount(data['account_equity'])+' USDT' if data.get('account_equity') is not None else '확인 중'),
                      '신규 진입 설정: '+('허용' if data.get('entry_enabled') else '중지'),
                      '현재 모델 진입 가능 상태: '+(str(data['qualifying_state_count'])+'개' if data.get('qualifying_state_count') is not None else '확인 중')])
        if data.get('live_sizing_model_compatible') is False:
            lines.append('현재 모델의 과거 주문 규모가 400 USDT 증거금 실매매 설정과 검증되지 않아 신규 주문을 내지 않습니다.')
        elif data.get('qualifying_state_count')==0:
            lines.append('현재 모델은 진입 조건이 0개여서 신규 주문을 내지 않습니다.')
        if data.get('stopped'):lines.append('마지막 기록이며 현재 관리 프로세스는 종료된 상태입니다.')
        elif event_ms is None and now_ms()-int(data.get('at',now_ms()))>120_000:lines.append('마지막 기록이 2분 이상 오래되어 현재 상태를 확인해야 합니다.')
        for s in SYMBOLS:
            p=data['positions'].get(s)
            position='보유 없음'
            if isinstance(p,dict):
                side='매수' if p.get('side')=='LONG' else '매도' if p.get('side')=='SHORT' else '방향 확인 중'
                position=f"{side} / 진입가 {p.get('entry')} / 수량 {p.get('qty', '확인 중')} / 손절가 {p.get('stop', '확인 중')}"
            lines.append(s+': '+position)
            info=data['probability'].get(s) or {}
            lines.append('  지금 상황: '+korean_reason(info.get('wait_reason','NO_COMPLETED_PRICE_VOLUME_EVENT')))
            if data.get('quote_errors',{}).get(s) or data.get('candle_errors',{}).get(s):lines.append('  시세·5분봉 확인 지연')
        if data.get('halt'):lines.append('중지 사유: '+korean_reason(data['halt']))
        if data.get('entry_blocks'):lines.append('추가 진입 제한: '+', '.join(korean_reason(x) for x in data['entry_blocks'][:3]))
    else:
        # Whitelist metadata; do not forward account snapshots or API responses.
        if 'symbol' in data:lines.append('종목: '+str(data['symbol']))
        if 'reason' in data:lines.append('사유: '+korean_reason(data['reason']))
        if 'cid' in data:lines.append('주문 식별값: '+str(data['cid']))
        if 'action' in data:lines.append('필요한 조치: 거래소의 실제 주문·보유 상태를 확인하세요.')
        if kind=='STOPPED':lines.append('기존 포지션이 있다면 거래소 손절을 확인하세요. 서버의 목표가·시간 청산 관리는 중단됩니다.')
    return '\n'.join(lines)[:3500]


def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS telegram_outbox (
    id INTEGER PRIMARY KEY, dedup TEXT UNIQUE NOT NULL, created INTEGER NOT NULL,
    text TEXT NOT NULL, delivered INTEGER, attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt INTEGER NOT NULL DEFAULT 0, last_error TEXT)''')
    db.execute('CREATE TABLE IF NOT EXISTS telegram_event_cursor (id INTEGER PRIMARY KEY CHECK(id=1), event_id INTEGER NOT NULL)');db.commit()
    db.execute('CREATE TABLE IF NOT EXISTS telegram_rate_limit (scope TEXT PRIMARY KEY, last_enqueued INTEGER NOT NULL, pending_id INTEGER)')
    if 'pending_id' not in {r[1] for r in db.execute('PRAGMA table_info(telegram_rate_limit)')}:
        db.execute('ALTER TABLE telegram_rate_limit ADD COLUMN pending_id INTEGER')
    db.commit()


def enqueue(store,kind,data,mode='live',event_ms=None,event_id=None):
    if kind not in EVENTS:return
    initialize(store.db)
    now=now_ms()
    if kind in ROUTINE_EVENTS and event_ms is not None and now-event_ms>15*60_000:return
    msg=render(kind,data,mode,event_ms=event_ms)
    # Event IDs make distinct fills unique, including two at the same price in one minute.
    keydata={'kind':kind,'event_id':event_id} if event_id is not None else {'kind':kind,'data':data,'bucket':now//60000}
    key=hashlib.sha256(json.dumps(keydata,sort_keys=True,allow_nan=False).encode()).hexdigest()
    scope='situation_summary' if kind=='HEARTBEAT' else 'routine_diagnostic' if kind in ROUTINE_EVENTS else (
          'urgent_'+kind+'_'+str(data.get('symbol','account')) if kind in ('URGENT_EXIT_REJECTED','LOCAL_TRAIL_STALE') else None)
    interval=15*60_000 if scope and scope.startswith('urgent_') else SIX_HOURS_MS
    with store.db:
        if scope:
            row=store.db.execute('SELECT last_enqueued,pending_id FROM telegram_rate_limit WHERE scope=?',(scope,)).fetchone()
            if row and now-row[0]<interval:return
            # A long outage retains only the newest unsent summary/diagnostic.
            if row and row[1] is not None:
                pending=store.db.execute('SELECT delivered FROM telegram_outbox WHERE id=?',(row[1],)).fetchone()
                if pending and pending[0] is None:
                    store.db.execute('UPDATE telegram_outbox SET text=?,created=?,next_attempt=0,attempts=0,last_error=NULL WHERE id=?',(msg,now,row[1]))
                    store.db.execute('UPDATE telegram_rate_limit SET last_enqueued=? WHERE scope=?',(now,scope))
                    return
        stored=store.db.execute('INSERT OR IGNORE INTO telegram_outbox(dedup,created,text) VALUES(?,?,?)',(key,now,msg))
        if scope and stored.rowcount:
            store.db.execute('INSERT OR REPLACE INTO telegram_rate_limit(scope,last_enqueued,pending_id) VALUES(?,?,?)',(scope,now,stored.lastrowid))


def send(settings,text,opener=None):
    opener=opener or urllib.request.urlopen
    payload=json.dumps({'chat_id':settings['chat'],'text':text,'disable_web_page_preview':True}).encode()
    req=urllib.request.Request('https://api.telegram.org/bot'+settings['token']+'/sendMessage',data=payload,headers={'Content-Type':'application/json'},method='POST')
    try:
        with opener(req,timeout=3) as response:result=json.loads(response.read())
        if not isinstance(result,dict) or result.get('ok') is not True:raise SafetyError('TELEGRAM_DELIVERY_REJECTED')
    except (urllib.error.URLError,TimeoutError,OSError,ValueError):raise SafetyError('TELEGRAM_DELIVERY_UNAVAILABLE') from None


class Outbox:
    def __init__(self,path,settings,log=lambda *_:None,opener=None):
        self.path=Path(path);self.settings=settings;self.log=log;self.opener=opener
        self.stop=threading.Event();self.thread=threading.Thread(target=self.run,name='telegram-outbox',daemon=True)
    def start(self):self.thread.start()
    def recover_events(self,db):
        row=db.execute('SELECT event_id FROM telegram_event_cursor WHERE id=1').fetchone();cursor=row[0] if row else 0
        events=db.execute('SELECT id,ts,kind,json FROM events WHERE id>? ORDER BY id LIMIT 100',(cursor,)).fetchall()
        proxy=type('OutboxStore',(),{'db':db})()
        for eid,stamp,kind,payload in events:
            enqueue(proxy,kind,json.loads(payload),self.path.stem,event_ms=stamp,event_id=eid)
            with db:db.execute('INSERT OR REPLACE INTO telegram_event_cursor VALUES(1,?)',(eid,))
    def step(self,db,now=None):
        self.recover_events(db)
        now=now if now is not None else now_ms()
        row=db.execute('SELECT id,text,attempts FROM telegram_outbox WHERE delivered IS NULL AND next_attempt<=? ORDER BY id LIMIT 1',(now,)).fetchone()
        if not row:return False
        oid,msg,attempts=row
        try:send(self.settings,msg,self.opener)
        except SafetyError as exc:
            retry=min(600000,5000*2**min(attempts,7))
            with db:db.execute('UPDATE telegram_outbox SET attempts=attempts+1,next_attempt=?,last_error=? WHERE id=?',(now+retry,str(exc),oid))
            self.log('TELEGRAM_RETRY_PENDING',{'attempt':attempts+1,'reason':str(exc)})
            return False
        with db:db.execute('UPDATE telegram_outbox SET delivered=?,attempts=attempts+1,last_error=NULL WHERE id=?',(now,oid))
        return True
    def run(self):
        db=sqlite3.connect(self.path,timeout=10)
        try:
            initialize(db)
            while not self.stop.is_set():
                try:self.step(db)
                except sqlite3.Error:self.log('TELEGRAM_RETRY_PENDING',{'reason':'SQLITE_TEMPORARY_UNAVAILABLE'})
                self.stop.wait(1)
        finally:db.close()


def summary(root,mode='live'):
    path=Path(root)/'data'/f'{mode}_status.json'
    if not path.exists():return f'[BTC·ETH 확률봇 {mode}] 아직 실행 기록이 없습니다.'
    st=json.loads(path.read_text());return render('HEARTBEAT',st,mode)
