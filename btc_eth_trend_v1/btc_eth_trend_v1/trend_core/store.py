import json,sqlite3
from pathlib import Path
from .core import now_ms

class Store:
    def __init__(self,path):
        Path(path).parent.mkdir(parents=True,exist_ok=True)
        self.db=sqlite3.connect(str(path),timeout=10)
        self.db.execute('PRAGMA journal_mode=WAL');self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, json TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, ts INTEGER, kind TEXT, json TEXT);
        CREATE TABLE IF NOT EXISTS signals (key TEXT PRIMARY KEY, ts INTEGER, json TEXT);
        CREATE TABLE IF NOT EXISTS trades (id TEXT PRIMARY KEY, closed INTEGER, json TEXT);''')
        self.db.commit()
    def get(self,key,default=None):
        r=self.db.execute('SELECT json FROM state WHERE key=?',(key,)).fetchone()
        return json.loads(r[0]) if r else default
    def set(self,key,value):
        with self.db:self.db.execute('INSERT OR REPLACE INTO state VALUES(?,?)',(key,json.dumps(value,allow_nan=False)))
    def event(self,kind,data):
        with self.db:self.db.execute('INSERT INTO events(ts,kind,json) VALUES(?,?,?)',(now_ms(),kind,json.dumps(data,allow_nan=False)))
    def seen(self,key):return bool(self.db.execute('SELECT 1 FROM signals WHERE key=?',(key,)).fetchone())
    def signal(self,key,data):
        with self.db:self.db.execute('INSERT OR IGNORE INTO signals VALUES(?,?,?)',(key,now_ms(),json.dumps(data,allow_nan=False)))
    def trade(self,t):
        with self.db:self.db.execute('INSERT OR REPLACE INTO trades VALUES(?,?,?)',(t['id'],t['closed'],json.dumps(t,allow_nan=False)))
    def finish(self,t,state):
        # Trade accounting and loss counters commit together, including after a crash.
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO trades VALUES(?,?,?)',(t['id'],t['closed'],json.dumps(t,allow_nan=False)))
            self.db.execute('INSERT OR REPLACE INTO state VALUES(?,?)',('engine',json.dumps(state,allow_nan=False)))
    def trades(self):return [json.loads(r[0]) for r in self.db.execute('SELECT json FROM trades ORDER BY closed,id')]
    def close(self):self.db.close()
