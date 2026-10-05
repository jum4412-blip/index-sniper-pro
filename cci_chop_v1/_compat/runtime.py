"""Local connection paths only; no legacy runtime is loaded."""
import json
from pathlib import Path
from .core import SafetyError

def connection(root):
    home=Path.home()
    c={'env':str(home/'index-sniper-pro/.env'),'legacy_root':str(home/'index-sniper-pro'),
       'trend_root':str(home/'btc_eth_trend_v1'),'basic_root':str(home/'btc_eth_larry_basic_v1'),'psych_root':str(home/'btc_eth_psych_trend_v1'),'trail3_root':str(home/'eth_larry_trail3_500')}
    path=Path(root)/'connection.json'
    if path.exists():
        v=json.loads(path.read_text())
        if not isinstance(v,dict) or set(v)-set(c) or not {'env','legacy_root','trend_root'}<=set(v):
            raise SafetyError('connection.json expects env, legacy_root, trend_root; basic_root optional')
        c.update(v)
    if any(not isinstance(v,str) or not v for v in c.values()):raise SafetyError('invalid connection path')
    return {k:str(Path(v).expanduser().resolve()) for k,v in c.items()}
