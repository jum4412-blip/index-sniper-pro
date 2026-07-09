#!/usr/bin/env bash
set -euo pipefail

echo "🚀 Applying BTC Quant v6.1 PRO Event Risk Guard..."

mkdir -p index_sniper config data/event_risk_pro logs exports
touch index_sniper/__init__.py

if [ ! -f config/event_risk_guard_pro.json ]; then
cat > config/event_risk_guard_pro.json <<'JSON'
{
  "version": "6.1-pro",
  "db_path": "data/event_risk_pro/events.sqlite3",
  "jsonl_path": "data/event_risk_pro/events.jsonl",
  "export_dir": "exports",
  "market_symbol": "BTCUSDT",
  "market_product_type": "usdt-futures",
  "lookback_hours": 6,
  "risk_half_life_hours": 3.0,
  "collect_interval_sec": 300,
  "reaction_windows_minutes": [5, 15, 60, 240, 1440],
  "reaction_label_max_events_per_run": 40,
  "reaction_delay_sec": 90,
  "providers": {
    "bitget": true,
    "gdelt": true,
    "cryptopanic": true,
    "rss": true,
    "defillama_hacks": true
  },
  "gdelt": {
    "query": "(bitcoin OR crypto OR cryptocurrency OR ethereum OR solana OR tether OR stablecoin OR binance OR bitget OR upbit OR sec OR cftc OR iran OR hormuz OR trump OR hack OR exploit)",
    "timespan": "1d",
    "maxrecords": 75
  },
  "bitget": {
    "language": "en_US",
    "limit": 20,
    "ann_types": ["latest_news", "coin_listings", "product_updates", "security", "api_trading", "maintenance_system_updates", "symbol_delisting"]
  },
  "cryptopanic": {
    "plan": "developer",
    "currencies": "BTC,ETH,SOL,XRP,TAO,HYPE,AAVE,USDT,USDC,BNB,DOGE,ADA,LINK",
    "filter": "rising"
  },
  "rss_urls": [],
  "thresholds": {
    "normal": 30,
    "caution": 50,
    "block_weak": 70,
    "block_new": 85
  }
}
JSON
fi

cat > index_sniper/event_risk_guard_pro.py <<'PY'
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BTC Quant v6.1 PRO Event Risk Guard

Pro 기능:
- Bitget/GDELT/CryptoPanic/RSS/DeFiLlama-Hacks 이벤트 수집
- 이벤트 정규화, 중복 제거, 리스크/변동성/불확실성/감성 점수화
- Bitget public candles 기반 BTC 반응 라벨링: 5m/15m/1h/4h/24h return
- SQLite + JSONL 누적 저장
- 현재 리스크 스냅샷, 가드 액션, 포지션 사이즈 배수 산출
- CSV export, provider doctor, manual event ingest

기존 매매/실주문 로직은 건드리지 않는다. 이 모듈은 관찰/가드 레이어다.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import email.utils
import hashlib
import html
import json
import math
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

KST = dt.timezone(dt.timedelta(hours=9), name="KST")
UTC = dt.timezone.utc

DEFAULT_CONFIG_PATH = os.getenv("EVENT_RISK_PRO_CONFIG", "config/event_risk_guard_pro.json")
USER_AGENT = os.getenv("EVENT_RISK_USER_AGENT", "index-sniper-pro-event-risk-guard/6.1-pro (+local collector)")

DEFAULT_CONFIG: Dict[str, Any] = {
    "version": "6.1-pro",
    "db_path": "data/event_risk_pro/events.sqlite3",
    "jsonl_path": "data/event_risk_pro/events.jsonl",
    "export_dir": "exports",
    "market_symbol": "BTCUSDT",
    "market_product_type": "usdt-futures",
    "lookback_hours": 6,
    "risk_half_life_hours": 3.0,
    "collect_interval_sec": 300,
    "reaction_windows_minutes": [5, 15, 60, 240, 1440],
    "reaction_label_max_events_per_run": 40,
    "reaction_delay_sec": 90,
    "providers": {"bitget": True, "gdelt": True, "cryptopanic": True, "rss": True, "defillama_hacks": True},
    "gdelt": {
        "query": "(bitcoin OR crypto OR cryptocurrency OR ethereum OR solana OR tether OR stablecoin OR binance OR bitget OR upbit OR sec OR cftc OR iran OR hormuz OR trump OR hack OR exploit)",
        "timespan": "1d",
        "maxrecords": 75,
    },
    "bitget": {
        "language": "en_US",
        "limit": 20,
        "ann_types": ["latest_news", "coin_listings", "product_updates", "security", "api_trading", "maintenance_system_updates", "symbol_delisting"],
    },
    "cryptopanic": {
        "plan": "developer",
        "currencies": "BTC,ETH,SOL,XRP,TAO,HYPE,AAVE,USDT,USDC,BNB,DOGE,ADA,LINK",
        "filter": "rising",
    },
    "rss_urls": [],
    "thresholds": {"normal": 30, "caution": 50, "block_weak": 70, "block_new": 85},
}

# category, event_type, regex, risk_delta, vol_delta, uncertainty_delta, sentiment_delta, direction, confidence
RULES: List[Tuple[str, str, str, int, int, int, int, str, float]] = [
    # stablecoins / market plumbing
    ("stablecoin", "depeg", r"\b(depeg|de-peg|depegged|loses peg|lost peg|peg break|redemption halt|redemptions halted|below \$?0\.9|디페그)\b", 95, 90, 80, -80, "risk_up", 0.94),
    ("stablecoin", "reserve_attestation_risk", r"\b(reserve concern|reserve report|attestation delay|commercial paper|stablecoin bill|테더|스테이블코인)\b", 35, 45, 40, -15, "uncertainty_up", 0.65),
    ("stablecoin", "mint_liquidity", r"\b(usdt minted|usdc minted|stablecoin mint|treasury minted|issued.*usdt|발행)\b", -5, 45, 25, 20, "liquidity_up", 0.58),
    ("stablecoin", "burn_liquidity", r"\b(usdt burn|usdc burn|stablecoin burn|redeemed.*usdt|소각)\b", 20, 40, 25, -10, "liquidity_down", 0.58),

    # exchanges and operations
    ("exchange", "cex_hack_or_freeze", r"\b(exchange hack|cex hack|hot wallet hacked|withdrawal freeze|withdrawals frozen|withdrawals? suspended|출금 중단|입출금 중단)\b", 90, 80, 75, -80, "risk_up", 0.90),
    ("exchange", "delisting", r"\b(delisting|delist|remove.*trading pair|trading pair.*remove|trading pair.*delist|거래지원 종료|상장폐지)\b", 75, 70, 55, -55, "risk_up", 0.84),
    ("exchange", "listing", r"\b(new listing|will list|listing of|listed on|spot listing|futures listing|launchpool|launchpad|거래지원|신규 상장|상장)\b", 12, 72, 45, 30, "volatility_up", 0.72),
    ("exchange", "maintenance_api", r"\b(system maintenance|system upgrade|service outage|api maintenance|api update|api suspend|matching engine|order issue|점검|장애)\b", 45, 55, 55, -25, "operational_risk", 0.78),
    ("exchange", "proof_of_reserves", r"\b(proof of reserves|por|reserve proof|protection fund)\b", -10, 30, 20, 20, "risk_down", 0.56),

    # hacks and security
    ("security", "exploit", r"\b(hack|hacked|exploit|exploited|drained|stolen|breach|private key|oracle attack|flash loan attack|bridge exploit|rug pull|reentrancy|해킹|익스플로잇)\b", 78, 74, 70, -70, "risk_up", 0.88),
    ("security", "bridge_exploit", r"\b(bridge exploit|cross-chain bridge|wormhole|multichain|nomad|ronin|브릿지)\b", 82, 78, 78, -75, "risk_up", 0.84),
    ("security", "recovered_funds", r"\b(recovered funds|funds recovered|whitehat|returned funds|복구)\b", -15, 35, 25, 25, "risk_down_vol_up", 0.62),

    # macro / war / politics
    ("macro_war", "war_escalation", r"\b(war|airstrike|air strike|missile|attack|retaliation|bombing|military action|hormuz|strait of hormuz|iran|israel|ceasefire collapses|nuclear deal over|공습|미사일|전쟁|호르무즈)\b", 68, 78, 75, -45, "risk_up", 0.75),
    ("macro_war", "sanctions", r"\b(sanction|sanctions|embargo|oil tanker|nuclear sanctions|제재)\b", 55, 60, 62, -35, "risk_up", 0.72),
    ("macro_war", "deescalation", r"\b(ceasefire|truce|peace talks|negotiations resume|diplomatic talks|deal reached|휴전|협상 재개|대화 재개)\b", -20, 52, 42, 25, "risk_down_vol_up", 0.70),
    ("macro_policy", "policy_flip", r"\b(reverses|walks back|contradicts|changes tone|no talks|talks possible|deal is over|open to talks|말을 바꾸|입장 번복|협상 없다|협상 가능)\b", 38, 72, 82, -25, "uncertainty_up", 0.73),

    # regulation
    ("regulation", "enforcement", r"\b(sec|cftc|doj|fiu|fsc|financial services commission|lawsuit|sues|charges|charged|enforcement|investigation|subpoena|settlement|소송|기소|조사|금융위)\b", 58, 64, 65, -40, "risk_up", 0.80),
    ("regulation", "ban_restriction", r"\b(ban|banned|restriction|prohibit|illegal|criminal|license revoked|licence revoked|금지|제한|불법)\b", 76, 74, 75, -62, "risk_up", 0.82),
    ("regulation", "approval_clarity", r"\b(approved|approval|etf approved|spot etf|regulatory clarity|no-action|no action|safe harbor|승인|규제 명확성)\b", -25, 55, 35, 52, "risk_down_vol_up", 0.74),

    # institutions / ETF / treasury
    ("institution", "etf_inflow", r"\b(etf inflow|record inflow|institutional buying|treasury buys|adds bitcoin|bitcoin reserve|기관 매수|ETF 유입)\b", -18, 48, 28, 48, "positive", 0.66),
    ("institution", "etf_outflow", r"\b(etf outflow|record outflow|institutional selling|treasury sells|ETF 유출|기관 매도)\b", 38, 50, 35, -45, "risk_up", 0.66),
    ("institution", "major_partnership", r"\b(partnership|integrates bitcoin|accepts bitcoin|payment integration|custody launch|파트너십)\b", -5, 38, 25, 25, "positive", 0.56),

    # project events
    ("project", "token_unlock", r"\b(token unlock|unlocking|vesting|cliff unlock|토큰 언락|락업 해제)\b", 38, 62, 48, -25, "supply_risk", 0.72),
    ("project", "airdrop_mainnet_upgrade", r"\b(airdrop|claim opens|mainnet|upgrade|hard fork|migration|에어드랍|메인넷|하드포크)\b", 18, 50, 36, 5, "volatility_up", 0.62),

    # whale/liquidity proxies appearing in text feeds
    ("whale_flow", "exchange_inflow", r"\b(whale|large transfer|exchange inflow|sent to exchange|transferred to binance|transferred to bitget|transferred to coinbase|거래소 입금|고래)\b", 34, 52, 40, -16, "risk_up", 0.58),
    ("whale_flow", "exchange_outflow", r"\b(exchange outflow|withdrawn from exchange|moved from exchange|cold wallet|거래소 출금|콜드월렛)\b", -6, 38, 28, 20, "accumulation_possible", 0.55),

    # market stress
    ("market", "liquidation", r"\b(liquidation|liquidated|short squeeze|long squeeze|margin call|cascade|청산|숏스퀴즈|롱스퀴즈)\b", 48, 72, 58, -20, "volatility_up", 0.72),
    ("market", "crash_pump", r"\b(crash|plunge|collapse|surge|soar|rally|breakout|폭락|급등|돌파)\b", 28, 58, 40, 0, "volatility_up", 0.56),
]

SYMBOL_PATTERNS: List[Tuple[str, str]] = [
    ("BTC", r"\b(bitcoin|btc|btcusdt|xbt)\b"),
    ("ETH", r"\b(ethereum|ether|eth|ethusdt)\b"),
    ("SOL", r"\b(solana|sol|solusdt)\b"),
    ("XRP", r"\b(xrp|ripple)\b"),
    ("USDT", r"\b(usdt|tether)\b"),
    ("USDC", r"\b(usdc|circle)\b"),
    ("TAO", r"\b(tao|bittensor)\b"),
    ("HYPE", r"\b(hype|hyperliquid)\b"),
    ("AAVE", r"\b(aave)\b"),
    ("BNB", r"\b(bnb|binance coin)\b"),
    ("DOGE", r"\b(dogecoin|doge)\b"),
    ("ADA", r"\b(cardano|ada)\b"),
    ("LINK", r"\b(chainlink|link)\b"),
]

SOURCE_RANKS = {
    "bitget": 0.88,
    "defillama_hacks": 0.86,
    "cryptopanic": 0.64,
    "gdelt": 0.60,
    "rss": 0.56,
    "manual": 0.70,
}

WINDOW_LABELS = {5: "5m", 15: "15m", 60: "1h", 240: "4h", 1440: "24h"}


def deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: str = DEFAULT_CONFIG_PATH) -> Dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                cfg = deep_merge(cfg, json.load(f))
        except Exception as e:
            print(f"⚠️ config load failed: {path}: {e}", file=sys.stderr)
    # env overrides
    if os.getenv("EVENT_RISK_DB_PATH"):
        cfg["db_path"] = os.getenv("EVENT_RISK_DB_PATH")
    if os.getenv("EVENT_RISK_JSONL_PATH"):
        cfg["jsonl_path"] = os.getenv("EVENT_RISK_JSONL_PATH")
    if os.getenv("EVENT_RISK_INTERVAL_SEC"):
        cfg["collect_interval_sec"] = int(os.getenv("EVENT_RISK_INTERVAL_SEC", "300"))
    if os.getenv("EVENT_RISK_LOOKBACK_HOURS"):
        cfg["lookback_hours"] = float(os.getenv("EVENT_RISK_LOOKBACK_HOURS", "6"))
    if os.getenv("EVENT_RISK_GDELT_QUERY"):
        cfg.setdefault("gdelt", {})["query"] = os.getenv("EVENT_RISK_GDELT_QUERY")
    if os.getenv("EVENT_RISK_GDELT_TIMESPAN"):
        cfg.setdefault("gdelt", {})["timespan"] = os.getenv("EVENT_RISK_GDELT_TIMESPAN")
    if os.getenv("EVENT_RISK_GDELT_MAXRECORDS"):
        cfg.setdefault("gdelt", {})["maxrecords"] = int(os.getenv("EVENT_RISK_GDELT_MAXRECORDS", "75"))
    if os.getenv("CRYPTOPANIC_PLAN"):
        cfg.setdefault("cryptopanic", {})["plan"] = os.getenv("CRYPTOPANIC_PLAN")
    if os.getenv("CRYPTOPANIC_CURRENCIES"):
        cfg.setdefault("cryptopanic", {})["currencies"] = os.getenv("CRYPTOPANIC_CURRENCIES")
    if os.getenv("EVENT_RISK_RSS_URLS"):
        cfg["rss_urls"] = [u.strip() for u in os.getenv("EVENT_RISK_RSS_URLS", "").split(",") if u.strip()]
    return cfg


def now_utc() -> dt.datetime:
    return dt.datetime.now(UTC)


def iso_utc(ts: Optional[dt.datetime] = None) -> str:
    ts = ts or now_utc()
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def iso_kst(ts: Optional[dt.datetime] = None) -> str:
    ts = ts or now_utc()
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts.astimezone(KST).replace(microsecond=0).isoformat()


def parse_any_time(value: Any) -> dt.datetime:
    if value is None or value == "":
        return now_utc()
    try:
        iv = int(str(value).strip())
        if iv > 10_000_000_000:
            return dt.datetime.fromtimestamp(iv / 1000.0, UTC)
        if iv > 1_000_000_000:
            return dt.datetime.fromtimestamp(iv, UTC)
    except Exception:
        pass
    s = str(value).strip()
    m = re.match(r"^(\d{8})T(\d{6})Z?$", s)
    if m:
        return dt.datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S").replace(tzinfo=UTC)
    try:
        s2 = s.replace("Z", "+00:00") if s.endswith("Z") else s
        out = dt.datetime.fromisoformat(s2)
        if out.tzinfo is None:
            out = out.replace(tzinfo=UTC)
        return out.astimezone(UTC)
    except Exception:
        pass
    try:
        out = email.utils.parsedate_to_datetime(s)
        if out.tzinfo is None:
            out = out.replace(tzinfo=UTC)
        return out.astimezone(UTC)
    except Exception:
        return now_utc()


def normalize_text(s: Any) -> str:
    if s is None:
        return ""
    s = html.unescape(str(s))
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def compact_key(s: str) -> str:
    s = normalize_text(s).lower()
    s = re.sub(r"https?://\S+", "", s)
    s = re.sub(r"[^a-z0-9가-힣$%\. ]+", " ", s)
    s = re.sub(r"\b(the|a|an|on|to|of|and|for|in|with|by|from|is|are|will|says|said)\b", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:240]


def hash_id(*parts: Any) -> str:
    raw = "|".join(compact_key(str(p)) for p in parts if p is not None)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def clamp(x: float, lo: float = 0, hi: float = 100) -> int:
    return int(max(lo, min(hi, round(x))))


def http_get(url: str, params: Optional[Dict[str, Any]] = None, timeout: int = 12, retries: int = 2, accept: str = "application/json,text/plain,*/*") -> bytes:
    if params:
        qs = urllib.parse.urlencode(params, doseq=True)
        url = url + ("&" if "?" in url else "?") + qs
    last: Optional[Exception] = None
    for i in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except Exception as e:
            last = e
            if i < retries:
                time.sleep(0.5 * (2 ** i))
    raise last or RuntimeError("http_get failed")


def http_get_json(url: str, params: Optional[Dict[str, Any]] = None, timeout: int = 12, retries: int = 2) -> Any:
    data = http_get(url, params=params, timeout=timeout, retries=retries, accept="application/json,text/plain,*/*")
    return json.loads(data.decode("utf-8", errors="replace"))


def http_get_text(url: str, params: Optional[Dict[str, Any]] = None, timeout: int = 12, retries: int = 2) -> str:
    data = http_get(url, params=params, timeout=timeout, retries=retries, accept="application/rss+xml,application/atom+xml,text/xml,text/html,*/*")
    return data.decode("utf-8", errors="replace")


def extract_symbols(text: str) -> List[str]:
    low = text.lower()
    out: List[str] = []
    for sym, pat in SYMBOL_PATTERNS:
        if re.search(pat, low, re.I):
            out.append(sym)
    return sorted(set(out))


def money_magnitude_boost(text: str) -> Tuple[int, int, int]:
    """Return risk, vol, uncertainty boosts based on explicit USD loss/flow size."""
    text = text.lower()
    max_usd = 0.0
    for m in re.finditer(r"\$?\s*([0-9]+(?:\.[0-9]+)?)\s*(billion|bn|b|million|m|thousand|k)?", text, re.I):
        try:
            value = float(m.group(1))
        except Exception:
            continue
        unit = (m.group(2) or "").lower()
        if unit in ("billion", "bn", "b"):
            value *= 1_000_000_000
        elif unit in ("million", "m"):
            value *= 1_000_000
        elif unit in ("thousand", "k"):
            value *= 1_000
        # Ignore small bare numbers unless symbol has $ and larger than 1000
        if not m.group(0).strip().startswith("$") and not unit:
            continue
        max_usd = max(max_usd, value)
    if max_usd >= 500_000_000:
        return 30, 28, 25
    if max_usd >= 100_000_000:
        return 24, 23, 22
    if max_usd >= 50_000_000:
        return 18, 20, 18
    if max_usd >= 10_000_000:
        return 12, 15, 13
    if max_usd >= 1_000_000:
        return 6, 10, 8
    return 0, 0, 0


def score_text(title: str, body: str = "", provider: str = "", source_type: str = "") -> Dict[str, Any]:
    text = f"{title} {body}".strip()
    low = text.lower()
    risk = 0
    vol = 0
    uncertainty = 0
    sentiment = 0
    matched: List[Dict[str, Any]] = []
    family_scores: Dict[str, int] = {}
    type_scores: Dict[str, int] = {}
    dir_scores: Dict[str, float] = {}

    for family, etype, pat, r_delta, v_delta, u_delta, s_delta, direction, conf in RULES:
        if re.search(pat, low, flags=re.I):
            risk += r_delta
            vol += v_delta
            uncertainty += u_delta
            sentiment += s_delta
            family_scores[family] = family_scores.get(family, 0) + abs(r_delta) + abs(v_delta) + abs(u_delta)
            type_scores[etype] = type_scores.get(etype, 0) + abs(r_delta) + abs(v_delta) + abs(u_delta)
            dir_scores[direction] = dir_scores.get(direction, 0.0) + conf
            matched.append({
                "family": family,
                "event_type": etype,
                "risk_delta": r_delta,
                "vol_delta": v_delta,
                "uncertainty_delta": u_delta,
                "sentiment_delta": s_delta,
                "direction": direction,
                "confidence": conf,
            })

    br, bv, bu = money_magnitude_boost(text)
    risk += br
    vol += bv
    uncertainty += bu

    # provider/source baselines
    if provider == "bitget" and "security" in source_type:
        risk += 22; vol += 20; uncertainty += 18
    if provider == "bitget" and "symbol_delisting" in source_type:
        risk += 35; vol += 32; uncertainty += 22
    if provider == "bitget" and "coin_listings" in source_type:
        vol += 40; uncertainty += 18; sentiment += 10
    if provider == "bitget" and ("maintenance" in source_type or "api" in source_type):
        risk += 18; vol += 25; uncertainty += 25
    if provider == "gdelt" and re.search(r"\b(iran|hormuz|war|missile|israel|sanction|trump)\b", low, re.I):
        vol += 18; uncertainty += 22
    if provider == "cryptopanic":
        vol += 12; uncertainty += 10
    if provider == "defillama_hacks":
        risk += 22; vol += 18; uncertainty += 15; sentiment -= 35

    risk_score = clamp(risk)
    volatility_score = clamp(vol)
    uncertainty_score = clamp(uncertainty)
    sentiment_score = int(max(-100, min(100, round(sentiment))))
    source_rank = SOURCE_RANKS.get(provider, 0.50)

    if family_scores:
        event_family = max(family_scores, key=family_scores.get)
    elif provider == "defillama_hacks":
        event_family = "security"
    else:
        event_family = "general"
    event_type = max(type_scores, key=type_scores.get) if type_scores else ("exploit" if provider == "defillama_hacks" else "news")
    direction = max(dir_scores, key=dir_scores.get) if dir_scores else "unknown"

    confidence = 0.38
    if matched:
        confidence = max(0.38, min(0.96, sum(m["confidence"] for m in matched) / len(matched)))
    confidence = max(confidence, source_rank - 0.08)

    impact_score = clamp(risk_score * 0.58 + volatility_score * 0.23 + uncertainty_score * 0.19)

    return {
        "risk_score": risk_score,
        "volatility_score": volatility_score,
        "uncertainty_score": uncertainty_score,
        "sentiment_score": sentiment_score,
        "event_family": event_family,
        "event_type": event_type,
        "direction": direction,
        "confidence": round(confidence, 3),
        "source_rank": round(source_rank, 3),
        "impact_score": impact_score,
        "symbols": extract_symbols(text),
        "matched_rules": matched,
    }


def init_db(cfg: Dict[str, Any]) -> sqlite3.Connection:
    path = cfg["db_path"]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA synchronous=NORMAL;")
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS events (
            event_id TEXT PRIMARY KEY,
            fingerprint TEXT NOT NULL,
            ts_utc TEXT NOT NULL,
            ts_kst TEXT NOT NULL,
            provider TEXT NOT NULL,
            source TEXT NOT NULL,
            source_type TEXT NOT NULL,
            title TEXT NOT NULL,
            body TEXT,
            url TEXT,
            symbols_json TEXT,
            event_family TEXT,
            event_type TEXT,
            direction TEXT,
            risk_score INTEGER NOT NULL,
            volatility_score INTEGER NOT NULL,
            uncertainty_score INTEGER NOT NULL,
            sentiment_score INTEGER NOT NULL,
            source_rank REAL NOT NULL,
            confidence REAL NOT NULL,
            impact_score INTEGER NOT NULL,
            raw_json TEXT,
            first_seen_utc TEXT NOT NULL,
            last_seen_utc TEXT NOT NULL,
            seen_count INTEGER NOT NULL DEFAULT 1
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS market_reactions (
            event_id TEXT NOT NULL,
            market_symbol TEXT NOT NULL,
            price_t0 REAL,
            reaction_json TEXT NOT NULL,
            matured_windows_json TEXT NOT NULL,
            status TEXT NOT NULL,
            error TEXT,
            updated_utc TEXT NOT NULL,
            PRIMARY KEY(event_id, market_symbol)
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS snapshots (
            snapshot_id TEXT PRIMARY KEY,
            ts_utc TEXT NOT NULL,
            ts_kst TEXT NOT NULL,
            lookback_hours REAL NOT NULL,
            overall_risk_score INTEGER NOT NULL,
            max_event_risk INTEGER NOT NULL,
            volatility_score INTEGER NOT NULL,
            uncertainty_score INTEGER NOT NULL,
            policy_flip_score INTEGER NOT NULL,
            cluster_score INTEGER NOT NULL,
            action TEXT NOT NULL,
            position_size_multiplier REAL NOT NULL,
            block_weak INTEGER NOT NULL,
            block_new_entries INTEGER NOT NULL,
            counts_json TEXT NOT NULL,
            reasons_json TEXT NOT NULL,
            top_events_json TEXT NOT NULL
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS provider_runs (
            run_id TEXT PRIMARY KEY,
            ts_utc TEXT NOT NULL,
            provider TEXT NOT NULL,
            status TEXT NOT NULL,
            fetched_count INTEGER NOT NULL,
            inserted_count INTEGER NOT NULL,
            skipped_count INTEGER NOT NULL,
            error TEXT
        )
        """
    )
    con.execute("CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts_utc)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_events_provider ON events(provider, ts_utc)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_events_score ON events(risk_score, volatility_score, uncertainty_score)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_events_fingerprint ON events(fingerprint)")
    con.commit()
    return con


def build_event(provider: str, source: str, source_type: str, title: str, body: str = "", url: str = "", ts: Any = None, external_id: Any = None, raw: Any = None) -> Dict[str, Any]:
    title = normalize_text(title)
    body = normalize_text(body)
    url = normalize_text(url)
    dt_utc = parse_any_time(ts)
    scored = score_text(title, body, provider=provider, source_type=source_type)
    fingerprint = compact_key(title)
    return {
        "event_id": hash_id(provider, source, source_type, external_id or url or title, title),
        "fingerprint": fingerprint,
        "ts_utc": iso_utc(dt_utc),
        "ts_kst": iso_kst(dt_utc),
        "provider": provider,
        "source": source,
        "source_type": source_type,
        "title": title[:700],
        "body": body[:1600],
        "url": url[:1000],
        "symbols": scored["symbols"],
        "event_family": scored["event_family"],
        "event_type": scored["event_type"],
        "direction": scored["direction"],
        "risk_score": scored["risk_score"],
        "volatility_score": scored["volatility_score"],
        "uncertainty_score": scored["uncertainty_score"],
        "sentiment_score": scored["sentiment_score"],
        "source_rank": scored["source_rank"],
        "confidence": scored["confidence"],
        "impact_score": scored["impact_score"],
        "matched_rules": scored["matched_rules"],
        "raw": raw or {},
    }


def insert_event(con: sqlite3.Connection, cfg: Dict[str, Any], event: Dict[str, Any]) -> bool:
    eid = event["event_id"]
    now = iso_utc()
    row = con.execute("SELECT seen_count FROM events WHERE event_id = ?", (eid,)).fetchone()
    if row:
        con.execute("UPDATE events SET last_seen_utc = ?, seen_count = seen_count + 1 WHERE event_id = ?", (now, eid))
        return False
    # Near-duplicate protection: same compact title in same provider within 1 day.
    recent_dup = con.execute(
        "SELECT event_id FROM events WHERE provider = ? AND fingerprint = ? AND ts_utc >= ? LIMIT 1",
        (event["provider"], event["fingerprint"], iso_utc(now_utc() - dt.timedelta(days=1))),
    ).fetchone()
    if recent_dup:
        con.execute("UPDATE events SET last_seen_utc = ?, seen_count = seen_count + 1 WHERE event_id = ?", (now, recent_dup[0]))
        return False

    con.execute(
        """
        INSERT INTO events (
            event_id, fingerprint, ts_utc, ts_kst, provider, source, source_type,
            title, body, url, symbols_json, event_family, event_type, direction,
            risk_score, volatility_score, uncertainty_score, sentiment_score,
            source_rank, confidence, impact_score, raw_json,
            first_seen_utc, last_seen_utc, seen_count
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
        """,
        (
            eid, event["fingerprint"], event["ts_utc"], event["ts_kst"], event["provider"], event["source"], event["source_type"],
            event["title"], event.get("body", ""), event.get("url", ""), json.dumps(event.get("symbols", []), ensure_ascii=False),
            event.get("event_family", "general"), event.get("event_type", "news"), event.get("direction", "unknown"),
            int(event.get("risk_score", 0)), int(event.get("volatility_score", 0)), int(event.get("uncertainty_score", 0)), int(event.get("sentiment_score", 0)),
            float(event.get("source_rank", 0.5)), float(event.get("confidence", 0.5)), int(event.get("impact_score", 0)),
            json.dumps(event.get("raw", {}), ensure_ascii=False, sort_keys=True), now, now,
        ),
    )
    jsonl_path = cfg["jsonl_path"]
    os.makedirs(os.path.dirname(jsonl_path), exist_ok=True)
    with open(jsonl_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
    return True


def log_provider_run(con: sqlite3.Connection, provider: str, status: str, fetched: int, inserted: int, skipped: int, error: str = "") -> None:
    con.execute(
        "INSERT OR REPLACE INTO provider_runs(run_id, ts_utc, provider, status, fetched_count, inserted_count, skipped_count, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (hash_id("provider", provider, iso_utc(now_utc()), status, error[:80]), iso_utc(), provider, status, fetched, inserted, skipped, error[:2000]),
    )


# ---------- Providers ----------

def collect_bitget(cfg: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[str]]:
    events: List[Dict[str, Any]] = []
    errors: List[str] = []
    bcfg = cfg.get("bitget", {})
    base = "https://api.bitget.com/api/v2/public/annoucements"  # official spelling
    ann_types = list(bcfg.get("ann_types") or [])
    # Also query without annType to avoid missing general notices.
    query_specs = [None] + ann_types
    for ann_type in query_specs:
        try:
            params = {"language": bcfg.get("language", "en_US"), "limit": int(bcfg.get("limit", 20))}
            if ann_type:
                params["annType"] = ann_type
            data = http_get_json(base, params=params)
            raw_items = data.get("data", []) if isinstance(data, dict) else []
            if isinstance(raw_items, dict):
                raw_items = raw_items.get("list") or raw_items.get("items") or raw_items.get("notices") or []
            for item in raw_items if isinstance(raw_items, list) else []:
                if not isinstance(item, dict):
                    continue
                title = item.get("annTitle") or item.get("title") or item.get("name") or ""
                if not title:
                    continue
                body = item.get("annDesc") or item.get("desc") or item.get("summary") or ""
                url = item.get("annUrl") or item.get("url") or ""
                ctime = item.get("cTime") or item.get("annTime") or item.get("publishTime") or item.get("createdTime")
                stype = f"bitget_{ann_type or 'all'}"
                events.append(build_event("bitget", "Bitget", stype, title, body=body, url=url, ts=ctime, external_id=item.get("annId") or item.get("id") or url, raw=item))
        except Exception as e:
            errors.append(f"Bitget {ann_type or 'all'}: {type(e).__name__}: {e}")
    return events, errors


def collect_gdelt(cfg: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[str]]:
    events: List[Dict[str, Any]] = []
    errors: List[str] = []
    gcfg = cfg.get("gdelt", {})
    params = {
        "query": gcfg.get("query", DEFAULT_CONFIG["gdelt"]["query"]),
        "mode": "ArtList",
        "format": "json",
        "maxrecords": int(gcfg.get("maxrecords", 75)),
        "timespan": gcfg.get("timespan", "1d"),
        "sort": "DateDesc",
    }
    try:
        data = http_get_json("https://api.gdeltproject.org/api/v2/doc/doc", params=params)
        for item in data.get("articles", []) if isinstance(data, dict) else []:
            if not isinstance(item, dict):
                continue
            title = item.get("title") or ""
            if not title:
                continue
            domain = item.get("domain") or "news"
            body = " ".join(str(item.get(k, "")) for k in ("domain", "sourcecountry", "language"))
            stype = "gdelt_macro" if re.search(r"\b(iran|hormuz|war|missile|trump|israel|sanction|ceasefire)\b", title, re.I) else "gdelt_crypto"
            events.append(build_event("gdelt", f"GDELT/{domain}", stype, title, body=body, url=item.get("url") or "", ts=item.get("seendate"), external_id=item.get("url") or title, raw=item))
    except Exception as e:
        errors.append(f"GDELT: {type(e).__name__}: {e}")
    return events, errors


def collect_cryptopanic(cfg: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[str]]:
    events: List[Dict[str, Any]] = []
    errors: List[str] = []
    token = os.getenv("CRYPTOPANIC_TOKEN", "").strip()
    if not token:
        return events, ["CryptoPanic skipped: set CRYPTOPANIC_TOKEN in .env"]
    pcfg = cfg.get("cryptopanic", {})
    plan = pcfg.get("plan", "developer")
    params = {
        "auth_token": token,
        "currencies": pcfg.get("currencies", DEFAULT_CONFIG["cryptopanic"]["currencies"]),
        "kind": "news",
        "public": "true",
    }
    # Some plans support filter; harmless if ignored by provider, but avoid empty value.
    if pcfg.get("filter"):
        params["filter"] = pcfg.get("filter")
    try:
        data = http_get_json(f"https://cryptopanic.com/api/{plan}/v2/posts/", params=params)
        for item in data.get("results", []) if isinstance(data, dict) else []:
            if not isinstance(item, dict):
                continue
            title = item.get("title") or ""
            if not title:
                continue
            src = item.get("source") or {}
            source_name = src.get("title") if isinstance(src, dict) else "unknown"
            currencies: List[str] = []
            for c in item.get("currencies", []) or []:
                if isinstance(c, dict) and c.get("code"):
                    currencies.append(str(c.get("code")).upper())
            votes = item.get("votes") or {}
            body = " ".join(currencies + [json.dumps(votes, ensure_ascii=False) if votes else ""])
            ev = build_event("cryptopanic", f"CryptoPanic/{source_name or 'unknown'}", "cryptopanic", title, body=body, url=item.get("url") or "", ts=item.get("published_at") or item.get("created_at"), external_id=item.get("id") or item.get("slug") or item.get("url"), raw=item)
            if currencies:
                ev["symbols"] = sorted(set(ev.get("symbols", []) + currencies))
            events.append(ev)
    except Exception as e:
        errors.append(f"CryptoPanic: {type(e).__name__}: {e}")
    return events, errors


def collect_rss(cfg: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[str]]:
    events: List[Dict[str, Any]] = []
    errors: List[str] = []
    rss_urls = cfg.get("rss_urls") or []
    if not rss_urls:
        return events, []
    for feed_url in rss_urls:
        try:
            text = http_get_text(feed_url)
            root = ET.fromstring(text.encode("utf-8"))
            host = urllib.parse.urlparse(feed_url).netloc or "feed"
            for item in root.findall(".//item")[:40]:
                title = normalize_text(item.findtext("title"))
                if not title:
                    continue
                link = normalize_text(item.findtext("link"))
                desc = normalize_text(item.findtext("description"))
                pub = item.findtext("pubDate") or item.findtext("date") or item.findtext("published") or ""
                events.append(build_event("rss", f"RSS/{host}", "rss", title, body=desc, url=link, ts=pub, external_id=link or title, raw={"feed": feed_url}))
            ns = {"a": "http://www.w3.org/2005/Atom"}
            for entry in root.findall(".//a:entry", ns)[:40]:
                title = normalize_text(entry.findtext("a:title", default="", namespaces=ns))
                if not title:
                    continue
                link_el = entry.find("a:link", ns)
                link = link_el.get("href", "") if link_el is not None else ""
                summary = normalize_text(entry.findtext("a:summary", default="", namespaces=ns))
                updated = entry.findtext("a:updated", default="", namespaces=ns)
                events.append(build_event("rss", f"RSS/{host}", "rss_atom", title, body=summary, url=link, ts=updated, external_id=link or title, raw={"feed": feed_url}))
        except Exception as e:
            errors.append(f"RSS {feed_url}: {type(e).__name__}: {e}")
    return events, errors


def collect_defillama_hacks(cfg: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Best-effort DeFiLlama hacks adapter. If the public endpoint changes, it fails closed and records an error."""
    events: List[Dict[str, Any]] = []
    errors: List[str] = []
    candidate_urls = [
        "https://api.llama.fi/hacks",
        "https://api.llama.fi/hackData",
    ]
    data: Any = None
    used = ""
    for url in candidate_urls:
        try:
            data = http_get_json(url, timeout=12, retries=1)
            used = url
            break
        except Exception as e:
            errors.append(f"DeFiLlama candidate failed {url}: {type(e).__name__}: {e}")
    if data is None:
        return events, errors[:2]

    if isinstance(data, dict):
        items = data.get("hacks") or data.get("data") or data.get("items") or []
    elif isinstance(data, list):
        items = data
    else:
        items = []
    for item in items[:80] if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        name = item.get("name") or item.get("protocol") or item.get("project") or item.get("target") or "Unknown protocol"
        amount = item.get("amount") or item.get("amountLost") or item.get("amount_usd") or item.get("usd") or item.get("fundsLost") or ""
        chain = item.get("chain") or item.get("chains") or ""
        technique = item.get("technique") or item.get("classification") or item.get("category") or ""
        date_value = item.get("date") or item.get("timestamp") or item.get("time") or item.get("createdAt")
        title = f"DeFi hack/exploit: {name} lost {amount} USD"
        body = f"chain={chain} technique={technique} source={used}"
        events.append(build_event("defillama_hacks", "DeFiLlama/Hacks", "defillama_hacks", title, body=body, url="https://defillama.com/hacks", ts=date_value, external_id=item.get("id") or f"{name}-{date_value}-{amount}", raw=item))
    return events, errors[:2]


PROVIDER_FUNCS = {
    "bitget": collect_bitget,
    "gdelt": collect_gdelt,
    "cryptopanic": collect_cryptopanic,
    "rss": collect_rss,
    "defillama_hacks": collect_defillama_hacks,
}


def collect_all(cfg: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Dict[str, List[str]]]:
    all_events: List[Dict[str, Any]] = []
    errors_by_provider: Dict[str, List[str]] = {}
    providers_cfg = cfg.get("providers", {})
    for name, fn in PROVIDER_FUNCS.items():
        if not providers_cfg.get(name, False):
            continue
        try:
            evs, errs = fn(cfg)
            all_events.extend(evs)
            if errs:
                errors_by_provider[name] = errs
        except Exception as e:
            errors_by_provider[name] = [f"{type(e).__name__}: {e}"]
    return all_events, errors_by_provider


# ---------- Market reaction labeling ----------

def bitget_candle_granularity(window_min: int) -> str:
    if window_min <= 60:
        return "1m"
    if window_min <= 360:
        return "5m"
    return "15m"


def fetch_bitget_close_near(ts: dt.datetime, cfg: Dict[str, Any], granularity: str = "1m") -> Optional[float]:
    symbol = cfg.get("market_symbol", "BTCUSDT")
    product_type = cfg.get("market_product_type", "usdt-futures")
    # Fetch a small range around target timestamp and choose the nearest candle close.
    if granularity == "1m":
        pad_min = 8
    elif granularity == "5m":
        pad_min = 25
    else:
        pad_min = 75
    start_ms = int((ts - dt.timedelta(minutes=pad_min)).timestamp() * 1000)
    end_ms = int((ts + dt.timedelta(minutes=pad_min)).timestamp() * 1000)
    params = {
        "symbol": symbol,
        "granularity": granularity,
        "startTime": str(start_ms),
        "endTime": str(end_ms),
        "limit": "100",
        "productType": product_type,
    }
    data = http_get_json("https://api.bitget.com/api/v2/mix/market/history-candles", params=params, timeout=10, retries=1)
    rows = data.get("data", data) if isinstance(data, dict) else data
    if not rows:
        return None
    target_ms = int(ts.timestamp() * 1000)
    best: Optional[Tuple[int, float]] = None
    for row in rows:
        try:
            # Bitget candle row: [timestamp, open, high, low, close, ...]
            ms = int(float(row[0]))
            close = float(row[4])
        except Exception:
            continue
        dist = abs(ms - target_ms)
        if best is None or dist < best[0]:
            best = (dist, close)
    return best[1] if best else None


def label_market_reactions(con: sqlite3.Connection, cfg: Dict[str, Any]) -> Tuple[int, int, List[str]]:
    symbol = cfg.get("market_symbol", "BTCUSDT")
    windows = [int(x) for x in cfg.get("reaction_windows_minutes", [5, 15, 60, 240, 1440])]
    max_events = int(cfg.get("reaction_label_max_events_per_run", 40))
    delay_sec = int(cfg.get("reaction_delay_sec", 90))
    now = now_utc()
    errors: List[str] = []
    updated = 0
    pending = 0

    cur = con.execute(
        """
        SELECT e.event_id, e.ts_utc, e.title, r.price_t0, r.reaction_json, r.matured_windows_json
        FROM events e
        LEFT JOIN market_reactions r ON r.event_id = e.event_id AND r.market_symbol = ?
        WHERE e.ts_utc <= ?
        ORDER BY e.ts_utc DESC
        LIMIT ?
        """,
        (symbol, iso_utc(now - dt.timedelta(minutes=5)), max_events),
    )
    rows = cur.fetchall()
    for event_id, ts_utc, title, price_t0_existing, reaction_json, matured_json in rows:
        event_ts = parse_any_time(ts_utc)
        reaction: Dict[str, Any] = {}
        matured: List[str] = []
        if reaction_json:
            try:
                reaction = json.loads(reaction_json)
            except Exception:
                reaction = {}
        if matured_json:
            try:
                matured = list(json.loads(matured_json))
            except Exception:
                matured = []
        try:
            price_t0 = float(price_t0_existing) if price_t0_existing is not None else None
            if price_t0 is None:
                price_t0 = fetch_bitget_close_near(event_ts, cfg, granularity="1m")
            if price_t0 is None:
                pending += 1
                con.execute(
                    "INSERT OR REPLACE INTO market_reactions(event_id, market_symbol, price_t0, reaction_json, matured_windows_json, status, error, updated_utc) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (event_id, symbol, None, json.dumps(reaction, ensure_ascii=False), json.dumps(matured, ensure_ascii=False), "pending_price_t0", "price_t0 unavailable", iso_utc()),
                )
                continue
            any_new = False
            for w in windows:
                label = WINDOW_LABELS.get(w, f"{w}m")
                if label in reaction:
                    continue
                mature_at = event_ts + dt.timedelta(minutes=w, seconds=delay_sec)
                if now < mature_at:
                    pending += 1
                    continue
                px = fetch_bitget_close_near(event_ts + dt.timedelta(minutes=w), cfg, granularity=bitget_candle_granularity(w))
                if px is None:
                    continue
                ret = (px / price_t0 - 1.0) * 100.0
                reaction[label] = {"price": px, "return_pct": round(ret, 5), "target_ts_utc": iso_utc(event_ts + dt.timedelta(minutes=w))}
                if label not in matured:
                    matured.append(label)
                any_new = True
            status = "matured" if len(matured) >= len(windows) else "partial" if matured else "pending"
            con.execute(
                "INSERT OR REPLACE INTO market_reactions(event_id, market_symbol, price_t0, reaction_json, matured_windows_json, status, error, updated_utc) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (event_id, symbol, price_t0, json.dumps(reaction, ensure_ascii=False, sort_keys=True), json.dumps(sorted(set(matured)), ensure_ascii=False), status, "", iso_utc()),
            )
            if any_new:
                updated += 1
        except Exception as e:
            errors.append(f"reaction {event_id} {title[:50]}: {type(e).__name__}: {e}")
            con.execute(
                "INSERT OR REPLACE INTO market_reactions(event_id, market_symbol, price_t0, reaction_json, matured_windows_json, status, error, updated_utc) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (event_id, symbol, price_t0_existing, reaction_json or "{}", matured_json or "[]", "error", str(e)[:1000], iso_utc()),
            )
    con.commit()
    return updated, pending, errors


# ---------- Snapshot / Guard ----------

def fetch_recent_events(con: sqlite3.Connection, lookback_hours: float, limit: int = 200) -> List[Dict[str, Any]]:
    cutoff = now_utc() - dt.timedelta(hours=lookback_hours)
    cur = con.execute(
        """
        SELECT event_id, ts_utc, ts_kst, provider, source, source_type, title, url,
               symbols_json, event_family, event_type, direction,
               risk_score, volatility_score, uncertainty_score, sentiment_score,
               source_rank, confidence, impact_score
        FROM events
        WHERE ts_utc >= ?
        ORDER BY impact_score DESC, risk_score DESC, ts_utc DESC
        LIMIT ?
        """,
        (iso_utc(cutoff), limit),
    )
    cols = [d[0] for d in cur.description]
    out: List[Dict[str, Any]] = []
    for row in cur.fetchall():
        d = dict(zip(cols, row))
        try:
            d["symbols"] = json.loads(d.pop("symbols_json") or "[]")
        except Exception:
            d["symbols"] = []
        out.append(d)
    return out


def detect_policy_flip(events: List[Dict[str, Any]]) -> int:
    # Simple but useful: mixed escalation/de-escalation/policy-flip signals inside macro/regulation within lookback.
    macro = [e for e in events if e.get("event_family") in ("macro_war", "macro_policy", "regulation")]
    dirs = {e.get("direction") for e in macro}
    titles = " ".join(e.get("title", "") for e in macro).lower()
    score = 0
    if "risk_up" in dirs and ("risk_down_vol_up" in dirs or "positive" in dirs):
        score += 35
    if "uncertainty_up" in dirs:
        score += 35
    if re.search(r"\b(no talks|deal is over|attack|airstrike|sanction|war)\b", titles) and re.search(r"\b(talks possible|open to talks|ceasefire|negotiations resume|deal reached)\b", titles):
        score += 35
    return clamp(score)


def compute_snapshot(con: sqlite3.Connection, cfg: Dict[str, Any], lookback_hours: Optional[float] = None) -> Dict[str, Any]:
    lookback = float(lookback_hours if lookback_hours is not None else cfg.get("lookback_hours", 6))
    half_life = float(cfg.get("risk_half_life_hours", 3.0))
    evs = fetch_recent_events(con, lookback, limit=300)
    now = now_utc()
    weighted_risk = 0.0
    weighted_vol = 0.0
    weighted_unc = 0.0
    max_risk = 0
    max_impact = 0
    counts: Dict[str, int] = {}
    high_recent = 0

    for e in evs:
        ts = parse_any_time(e["ts_utc"])
        age_h = max(0.0, (now - ts).total_seconds() / 3600.0)
        decay = math.exp(-age_h / max(0.1, half_life))
        confidence = float(e.get("confidence", 0.5))
        source_rank = float(e.get("source_rank", 0.5))
        weight = decay * (0.55 * confidence + 0.45 * source_rank)
        risk = int(e.get("risk_score", 0))
        vol = int(e.get("volatility_score", 0))
        unc = int(e.get("uncertainty_score", 0))
        impact = int(e.get("impact_score", 0))
        weighted_risk += risk * weight
        weighted_vol += vol * weight
        weighted_unc += unc * weight
        max_risk = max(max_risk, risk)
        max_impact = max(max_impact, impact)
        family = e.get("event_family") or "general"
        counts[family] = counts.get(family, 0) + 1
        if age_h <= 1.0 and impact >= 50:
            high_recent += 1

    policy_flip = detect_policy_flip(evs)
    cluster_score = clamp(max(0, high_recent - 1) * 12 + sum(1 for v in counts.values() if v >= 5) * 8)
    base = min(100.0, weighted_risk / 2.3 if evs else 0.0)
    vol_score = clamp(weighted_vol / 2.5 if evs else 0.0)
    unc_score = clamp(weighted_unc / 2.4 if evs else 0.0)
    overall = clamp(max(max_impact * 0.88, base) + 0.18 * vol_score + 0.22 * unc_score + 0.25 * policy_flip + 0.25 * cluster_score)

    th = cfg.get("thresholds", DEFAULT_CONFIG["thresholds"])
    if overall >= int(th.get("block_new", 85)):
        action = "BLOCK_ALL_NEW_ENTRIES"
        size = 0.0; block_weak = 1; block_new = 1
    elif overall >= int(th.get("block_weak", 70)):
        action = "BLOCK_NEW_ENTRIES_30_60M"
        size = 0.0; block_weak = 1; block_new = 1
    elif overall >= int(th.get("caution", 50)):
        action = "BLOCK_WEAK_SIGNALS_STRONG_ONLY_SIZE_HALF"
        size = 0.5; block_weak = 1; block_new = 0
    elif overall >= int(th.get("normal", 30)):
        action = "CAUTION_SIZE_0_7"
        size = 0.7; block_weak = 0; block_new = 0
    else:
        action = "NORMAL"
        size = 1.0; block_weak = 0; block_new = 0

    reasons: List[str] = []
    if evs:
        top = evs[:5]
        for e in top:
            reasons.append(f"{e.get('event_family')}/{e.get('event_type')} {e.get('risk_score')}/{e.get('volatility_score')}: {e.get('title')[:110]}")
    if policy_flip >= 35:
        reasons.append(f"policy_flip_score={policy_flip}")
    if cluster_score >= 12:
        reasons.append(f"event_cluster_score={cluster_score}")

    snap = {
        "snapshot_id": hash_id("snapshot", iso_utc(now), str(overall), str(len(evs))),
        "ts_utc": iso_utc(now),
        "ts_kst": iso_kst(now),
        "lookback_hours": lookback,
        "overall_risk_score": overall,
        "max_event_risk": max_risk,
        "max_impact_score": max_impact,
        "volatility_score": vol_score,
        "uncertainty_score": unc_score,
        "policy_flip_score": policy_flip,
        "cluster_score": cluster_score,
        "action": action,
        "position_size_multiplier": size,
        "block_weak": bool(block_weak),
        "block_new_entries": bool(block_new),
        "counts": counts,
        "reasons": reasons[:10],
        "top_events": evs[:12],
    }
    con.execute(
        """
        INSERT OR REPLACE INTO snapshots(
            snapshot_id, ts_utc, ts_kst, lookback_hours,
            overall_risk_score, max_event_risk, volatility_score, uncertainty_score,
            policy_flip_score, cluster_score, action, position_size_multiplier,
            block_weak, block_new_entries, counts_json, reasons_json, top_events_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            snap["snapshot_id"], snap["ts_utc"], snap["ts_kst"], lookback,
            snap["overall_risk_score"], snap["max_event_risk"], snap["volatility_score"], snap["uncertainty_score"],
            snap["policy_flip_score"], snap["cluster_score"], snap["action"], snap["position_size_multiplier"],
            int(snap["block_weak"]), int(snap["block_new_entries"]), json.dumps(counts, ensure_ascii=False, sort_keys=True),
            json.dumps(snap["reasons"], ensure_ascii=False), json.dumps(snap["top_events"], ensure_ascii=False, sort_keys=True),
        ),
    )
    con.commit()
    return snap


def get_event_guard_snapshot(lookback_hours: float = 6.0, config_path: str = DEFAULT_CONFIG_PATH) -> Dict[str, Any]:
    cfg = load_config(config_path)
    con = init_db(cfg)
    try:
        return compute_snapshot(con, cfg, lookback_hours=lookback_hours)
    finally:
        con.close()


def should_allow_signal(signal: str, base_size: float = 1.0, lookback_hours: float = 6.0, config_path: str = DEFAULT_CONFIG_PATH) -> Dict[str, Any]:
    """기존 봇에서 쓰기 쉬운 최종 게이트 함수."""
    snap = get_event_guard_snapshot(lookback_hours=lookback_hours, config_path=config_path)
    sig = (signal or "").upper()
    weak = sig.startswith("WEAK_") or sig in ("WEAK_LONG", "WEAK_SHORT")
    allowed = True
    reason = snap["action"]
    size_multiplier = float(snap.get("position_size_multiplier", 1.0))
    if snap.get("block_new_entries"):
        allowed = False
        size_multiplier = 0.0
    elif weak and snap.get("block_weak"):
        allowed = False
        size_multiplier = 0.0
        reason = "BLOCK_WEAK_SIGNAL_BY_EVENT_RISK"
    adjusted_size = base_size * size_multiplier
    return {"allowed": allowed, "adjusted_size": adjusted_size, "size_multiplier": size_multiplier, "reason": reason, "snapshot": snap}


# ---------- Commands ----------

def run_collect_once(cfg: Dict[str, Any], con: sqlite3.Connection) -> Tuple[int, int, Dict[str, List[str]]]:
    events, errors_by_provider = collect_all(cfg)
    by_provider: Dict[str, Dict[str, int]] = {}
    new_count = 0
    skipped_count = 0
    for ev in events:
        p = ev.get("provider", "unknown")
        by_provider.setdefault(p, {"fetched": 0, "inserted": 0, "skipped": 0})["fetched"] += 1
        try:
            if insert_event(con, cfg, ev):
                new_count += 1
                by_provider[p]["inserted"] += 1
            else:
                skipped_count += 1
                by_provider[p]["skipped"] += 1
        except Exception as e:
            errors_by_provider.setdefault(p, []).append(f"insert: {type(e).__name__}: {e}")
    for p, stats in by_provider.items():
        log_provider_run(con, p, "ok" if not errors_by_provider.get(p) else "partial", stats["fetched"], stats["inserted"], stats["skipped"], "; ".join(errors_by_provider.get(p, [])[:4]))
    for p, errs in errors_by_provider.items():
        if p not in by_provider:
            log_provider_run(con, p, "error" if not (len(errs)==1 and "skipped" in errs[0].lower()) else "skipped", 0, 0, 0, "; ".join(errs[:4]))
    con.commit()
    return new_count, skipped_count, errors_by_provider


def action_icon(score: int) -> str:
    if score >= 85:
        return "🔴"
    if score >= 70:
        return "🟠"
    if score >= 50:
        return "🟡"
    if score >= 30:
        return "🟤"
    return "🟢"


def print_report(snap: Dict[str, Any], new_count: int = 0, skipped_count: int = 0, reaction_updated: int = 0, reaction_pending: int = 0, errors_by_provider: Optional[Dict[str, List[str]]] = None, reaction_errors: Optional[List[str]] = None, cfg: Optional[Dict[str, Any]] = None) -> None:
    cfg = cfg or load_config()
    score = int(snap["overall_risk_score"])
    print(f"{action_icon(score)} BTC Quant v6.1 PRO Event Risk Guard")
    print(f"시각: {snap['ts_kst']}")
    print(f"상태: {snap['action']} / risk {score}/100 / vol {snap['volatility_score']}/100 / uncertainty {snap['uncertainty_score']}/100")
    print(f"가드: block_new={snap['block_new_entries']} / block_weak={snap['block_weak']} / size_x={snap['position_size_multiplier']}")
    print(f"추가점수: policy_flip {snap['policy_flip_score']} / cluster {snap['cluster_score']} / lookback {snap['lookback_hours']}h")
    print(f"수집: 신규 {new_count} / 중복 {skipped_count} / 반응라벨 업데이트 {reaction_updated} / 반응대기 {reaction_pending}")
    print(f"DB: {cfg['db_path']}")
    counts = snap.get("counts") or {}
    if counts:
        print("분류: " + ", ".join(f"{k}:{v}" for k, v in sorted(counts.items(), key=lambda x: x[1], reverse=True)[:10]))
    print("\nTop events:")
    if not snap.get("top_events"):
        print("- 최근 이벤트 없음")
    for e in snap.get("top_events", [])[:8]:
        syms = ",".join(e.get("symbols", [])) if e.get("symbols") else "-"
        print(f"- impact {e.get('impact_score')} | risk/vol/unc {e.get('risk_score')}/{e.get('volatility_score')}/{e.get('uncertainty_score')} | {e.get('provider')} | {e.get('event_family')}/{e.get('event_type')} | {syms}")
        print(f"  {e.get('title')}")
        if e.get("url"):
            print(f"  {e.get('url')}")
    if snap.get("reasons"):
        print("\nReasons:")
        for r in snap["reasons"][:6]:
            print(f"- {r}")
    all_errors: List[str] = []
    for p, errs in (errors_by_provider or {}).items():
        for err in errs:
            all_errors.append(f"{p}: {err}")
    for err in reaction_errors or []:
        all_errors.append(f"reaction: {err}")
    if all_errors:
        print("\n주의/스킵:")
        for err in all_errors[:10]:
            print(f"- {err}")
        if len(all_errors) > 10:
            print(f"- ... {len(all_errors) - 10} more")


def export_csv(cfg: Dict[str, Any], con: sqlite3.Connection, output: Optional[str] = None, limit: int = 5000) -> str:
    os.makedirs(cfg.get("export_dir", "exports"), exist_ok=True)
    if not output:
        output = os.path.join(cfg.get("export_dir", "exports"), f"event_risk_pro_export_{dt.datetime.now(KST).strftime('%Y%m%d_%H%M%S')}.csv")
    cur = con.execute(
        """
        SELECT e.event_id, e.ts_kst, e.provider, e.source, e.source_type, e.title, e.url,
               e.symbols_json, e.event_family, e.event_type, e.direction,
               e.risk_score, e.volatility_score, e.uncertainty_score, e.sentiment_score,
               e.source_rank, e.confidence, e.impact_score,
               r.price_t0, r.reaction_json, r.status
        FROM events e
        LEFT JOIN market_reactions r ON r.event_id = e.event_id AND r.market_symbol = ?
        ORDER BY e.ts_utc DESC
        LIMIT ?
        """,
        (cfg.get("market_symbol", "BTCUSDT"), limit),
    )
    fields = ["event_id", "ts_kst", "provider", "source", "source_type", "title", "url", "symbols", "event_family", "event_type", "direction", "risk_score", "volatility_score", "uncertainty_score", "sentiment_score", "source_rank", "confidence", "impact_score", "price_t0", "ret_5m", "ret_15m", "ret_1h", "ret_4h", "ret_24h", "reaction_status"]
    with open(output, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for row in cur.fetchall():
            (event_id, ts_kst, provider, source, source_type, title, url, symbols_json, event_family, event_type, direction, risk_score, vol_score, unc_score, sentiment_score, source_rank, confidence, impact_score, price_t0, reaction_json, status) = row
            try:
                symbols = ",".join(json.loads(symbols_json or "[]"))
            except Exception:
                symbols = ""
            try:
                reaction = json.loads(reaction_json or "{}")
            except Exception:
                reaction = {}
            out = {
                "event_id": event_id, "ts_kst": ts_kst, "provider": provider, "source": source, "source_type": source_type,
                "title": title, "url": url, "symbols": symbols, "event_family": event_family, "event_type": event_type, "direction": direction,
                "risk_score": risk_score, "volatility_score": vol_score, "uncertainty_score": unc_score, "sentiment_score": sentiment_score,
                "source_rank": source_rank, "confidence": confidence, "impact_score": impact_score, "price_t0": price_t0, "reaction_status": status or "",
            }
            for label in ("5m", "15m", "1h", "4h", "24h"):
                out[f"ret_{label}"] = reaction.get(label, {}).get("return_pct", "") if isinstance(reaction, dict) else ""
            w.writerow(out)
    return output


def doctor(cfg: Dict[str, Any]) -> int:
    print("🧪 Provider doctor")
    ok = 0
    bad = 0
    for name, fn in PROVIDER_FUNCS.items():
        if not cfg.get("providers", {}).get(name, False):
            print(f"- {name}: disabled")
            continue
        try:
            evs, errs = fn(cfg)
            status = "OK" if evs or (name == "cryptopanic" and errs and "skipped" in errs[0].lower()) or (name == "rss" and not cfg.get("rss_urls")) else "CHECK"
            if status == "OK": ok += 1
            else: bad += 1
            print(f"- {name}: {status} / fetched={len(evs)}")
            for err in errs[:3]:
                print(f"  · {err}")
        except Exception as e:
            bad += 1
            print(f"- {name}: FAIL / {type(e).__name__}: {e}")
    try:
        px = fetch_bitget_close_near(now_utc() - dt.timedelta(minutes=3), cfg, granularity="1m")
        print(f"- bitget_market_price: {'OK' if px else 'CHECK'} / price={px}")
    except Exception as e:
        bad += 1
        print(f"- bitget_market_price: FAIL / {type(e).__name__}: {e}")
    return 0 if bad == 0 else 1


def add_manual_event(cfg: Dict[str, Any], con: sqlite3.Connection, title: str, body: str = "", ts: str = "") -> None:
    ev = build_event("manual", "Manual", "manual", title, body=body, url="", ts=ts or now_utc(), external_id=f"manual-{title}-{ts or iso_utc()}", raw={"manual": True})
    inserted = insert_event(con, cfg, ev)
    con.commit()
    print(f"manual event {'inserted' if inserted else 'deduped'}: {ev['event_id']} / risk={ev['risk_score']} vol={ev['volatility_score']} unc={ev['uncertainty_score']}")


def run_once(cfg: Dict[str, Any], report_only: bool = False, label_reactions: bool = True) -> int:
    con = init_db(cfg)
    try:
        new_count = 0
        skipped_count = 0
        errors_by_provider: Dict[str, List[str]] = {}
        if not report_only:
            new_count, skipped_count, errors_by_provider = run_collect_once(cfg, con)
        reaction_updated = 0
        reaction_pending = 0
        reaction_errors: List[str] = []
        if label_reactions:
            reaction_updated, reaction_pending, reaction_errors = label_market_reactions(con, cfg)
        snap = compute_snapshot(con, cfg)
        print_report(snap, new_count, skipped_count, reaction_updated, reaction_pending, errors_by_provider, reaction_errors, cfg)
        return 0
    finally:
        con.close()


def run_loop(cfg: Dict[str, Any], interval_sec: Optional[int] = None) -> None:
    interval = int(interval_sec or cfg.get("collect_interval_sec", 300))
    while True:
        try:
            run_once(cfg, report_only=False, label_reactions=True)
        except KeyboardInterrupt:
            raise
        except Exception as e:
            print(f"Event Risk Guard PRO loop error: {type(e).__name__}: {e}", file=sys.stderr)
        print(f"\n--- sleep {interval}s ---\n", flush=True)
        time.sleep(interval)


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(description="BTC Quant v6.1 PRO Event Risk Guard")
    p.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    p.add_argument("--once", action="store_true", help="collect once, label reactions, print report")
    p.add_argument("--loop", action="store_true", help="run continuously")
    p.add_argument("--report", action="store_true", help="report from existing DB; also tries reaction labeling")
    p.add_argument("--no-label", action="store_true", help="skip market reaction labeling")
    p.add_argument("--export-csv", nargs="?", const="", help="export events+reactions to CSV")
    p.add_argument("--export-limit", type=int, default=5000)
    p.add_argument("--doctor", action="store_true", help="test providers")
    p.add_argument("--add-event", help="manually add an event title")
    p.add_argument("--body", default="", help="manual event body")
    p.add_argument("--ts", default="", help="manual event timestamp")
    p.add_argument("--interval-sec", type=int, default=None)
    args = p.parse_args(argv)

    cfg = load_config(args.config)
    if args.doctor:
        return doctor(cfg)
    con = init_db(cfg)
    con.close()
    if args.add_event:
        con = init_db(cfg)
        try:
            add_manual_event(cfg, con, args.add_event, body=args.body, ts=args.ts)
        finally:
            con.close()
        return run_once(cfg, report_only=True, label_reactions=not args.no_label)
    if args.export_csv is not None:
        con = init_db(cfg)
        try:
            out = export_csv(cfg, con, output=args.export_csv or None, limit=args.export_limit)
            print(out)
            return 0
        finally:
            con.close()
    if args.loop:
        run_loop(cfg, interval_sec=args.interval_sec)
        return 0
    if args.report:
        return run_once(cfg, report_only=True, label_reactions=not args.no_label)
    return run_once(cfg, report_only=False, label_reactions=not args.no_label)


if __name__ == "__main__":
    raise SystemExit(main())
PY

cat > run_v61_event_guard_pro_once.sh <<'SH'
#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [ -f .env ]; then set -a; source .env; set +a; fi
python3 -m index_sniper.event_risk_guard_pro --once
SH

cat > run_v61_event_guard_pro_report.sh <<'SH'
#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [ -f .env ]; then set -a; source .env; set +a; fi
python3 -m index_sniper.event_risk_guard_pro --report
SH

cat > run_v61_event_guard_pro_doctor.sh <<'SH'
#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [ -f .env ]; then set -a; source .env; set +a; fi
python3 -m index_sniper.event_risk_guard_pro --doctor
SH

cat > export_v61_event_guard_pro_csv.sh <<'SH'
#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [ -f .env ]; then set -a; source .env; set +a; fi
python3 -m index_sniper.event_risk_guard_pro --export-csv "$@"
SH

cat > start_v61_event_guard_pro.sh <<'SH'
#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
SESSION="${EVENT_RISK_PRO_SCREEN_NAME:-v61_event_guard_pro}"
INTERVAL="${EVENT_RISK_INTERVAL_SEC:-300}"
LOG="logs/v61_event_guard_pro.log"
mkdir -p logs

if command -v screen >/dev/null 2>&1; then
  if screen -list | grep -q "[.]${SESSION}[[:space:]]"; then
    echo "이미 실행 중: ${SESSION}"
    screen -ls | grep "${SESSION}" || true
    exit 0
  fi
  screen -dmS "${SESSION}" bash -lc "
    cd '$(pwd)'
    if [ -f .env ]; then set -a; source .env; set +a; fi
    while true; do
      python3 -m index_sniper.event_risk_guard_pro --once >> '${LOG}' 2>&1
      echo '--- sleep ${INTERVAL}s ---' >> '${LOG}'
      sleep '${INTERVAL}'
    done
  "
  echo "✅ v6.1 PRO Event Risk Guard 시작: screen=${SESSION}, interval=${INTERVAL}s"
else
  if [ -f logs/v61_event_guard_pro.pid ] && kill -0 "$(cat logs/v61_event_guard_pro.pid)" 2>/dev/null; then
    echo "이미 실행 중: pid=$(cat logs/v61_event_guard_pro.pid)"
    exit 0
  fi
  nohup bash -lc "
    cd '$(pwd)'
    if [ -f .env ]; then set -a; source .env; set +a; fi
    while true; do
      python3 -m index_sniper.event_risk_guard_pro --once >> '${LOG}' 2>&1
      echo '--- sleep ${INTERVAL}s ---' >> '${LOG}'
      sleep '${INTERVAL}'
    done
  " >/dev/null 2>&1 &
  echo $! > logs/v61_event_guard_pro.pid
  echo "✅ v6.1 PRO Event Risk Guard 시작: nohup pid=$(cat logs/v61_event_guard_pro.pid), interval=${INTERVAL}s"
fi

echo "로그 보기: tail -f ${LOG}"
echo "리포트: bash run_v61_event_guard_pro_report.sh"
SH

cat > stop_v61_event_guard_pro.sh <<'SH'
#!/usr/bin/env bash
set -euo pipefail
SESSION="${EVENT_RISK_PRO_SCREEN_NAME:-v61_event_guard_pro}"
if command -v screen >/dev/null 2>&1 && screen -list | grep -q "[.]${SESSION}[[:space:]]"; then
  screen -S "${SESSION}" -X quit
  echo "✅ stopped screen: ${SESSION}"
elif [ -f logs/v61_event_guard_pro.pid ] && kill -0 "$(cat logs/v61_event_guard_pro.pid)" 2>/dev/null; then
  kill "$(cat logs/v61_event_guard_pro.pid)"
  rm -f logs/v61_event_guard_pro.pid
  echo "✅ stopped nohup process"
else
  echo "실행 중인 PRO 세션 없음"
fi
SH

cat > event_risk_guard_pro.service.example <<'SERVICE'
# Optional systemd user service example
# cp event_risk_guard_pro.service.example ~/.config/systemd/user/event-risk-guard-pro.service
# systemctl --user daemon-reload
# systemctl --user enable --now event-risk-guard-pro
[Unit]
Description=BTC Quant v6.1 PRO Event Risk Guard
After=network-online.target

[Service]
Type=simple
WorkingDirectory=%h/index-sniper-pro
EnvironmentFile=-%h/index-sniper-pro/.env
ExecStart=/usr/bin/python3 -m index_sniper.event_risk_guard_pro --loop
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
SERVICE

cat > README_EVENT_RISK_GUARD_PRO_v61.md <<'MD'
# BTC Quant v6.1 PRO Event Risk Guard

기존 v6.0/v6.1 신호 위에 얹는 뉴스/이벤트 리스크 가드입니다. 실주문 로직은 자동으로 변경하지 않고, 먼저 데이터를 쌓고 검증하기 위한 구조입니다.

## 바로 실행

```bash
chmod +x run_v61_event_guard_pro_once.sh start_v61_event_guard_pro.sh stop_v61_event_guard_pro.sh run_v61_event_guard_pro_report.sh run_v61_event_guard_pro_doctor.sh export_v61_event_guard_pro_csv.sh
bash run_v61_event_guard_pro_once.sh
```

## 계속 수집

```bash
bash start_v61_event_guard_pro.sh
tail -f logs/v61_event_guard_pro.log
```

중지:

```bash
bash stop_v61_event_guard_pro.sh
```

## 상태 확인

```bash
bash run_v61_event_guard_pro_report.sh
bash run_v61_event_guard_pro_doctor.sh
```

## CSV 내보내기

```bash
bash export_v61_event_guard_pro_csv.sh
```

결과는 `exports/event_risk_pro_export_YYYYMMDD_HHMMSS.csv`에 저장됩니다.

## 저장 위치

```text
config/event_risk_guard_pro.json
data/event_risk_pro/events.sqlite3
data/event_risk_pro/events.jsonl
logs/v61_event_guard_pro.log
exports/*.csv
```

## Provider

기본 ON:

```text
Bitget official announcements
GDELT global news
DeFiLlama hacks, best-effort endpoint
CryptoPanic, CRYPTOPANIC_TOKEN 있을 때만 활성
RSS/Atom, EVENT_RISK_RSS_URLS 또는 config에 추가 시 활성
```

`.env` 예시:

```bash
CRYPTOPANIC_TOKEN=""
CRYPTOPANIC_PLAN="developer"
CRYPTOPANIC_CURRENCIES="BTC,ETH,SOL,XRP,TAO,HYPE,AAVE,USDT,USDC"
EVENT_RISK_INTERVAL_SEC="300"
EVENT_RISK_LOOKBACK_HOURS="6"
EVENT_RISK_RSS_URLS=""
```

## 가드 액션

```text
0~29   NORMAL
30~49  CAUTION_SIZE_0_7
50~69  BLOCK_WEAK_SIGNALS_STRONG_ONLY_SIZE_HALF
70~84  BLOCK_NEW_ENTRIES_30_60M
85~100 BLOCK_ALL_NEW_ENTRIES
```

## 기존 봇에 연결하는 함수

```python
from index_sniper.event_risk_guard_pro import should_allow_signal

result = should_allow_signal(signal="WEAK_LONG", base_size=1.0)
if not result["allowed"]:
    print("차단:", result["reason"])
else:
    size = result["adjusted_size"]
```

또는 스냅샷만:

```python
from index_sniper.event_risk_guard_pro import get_event_guard_snapshot

guard = get_event_guard_snapshot(lookback_hours=6)
print(guard["overall_risk_score"], guard["action"])
```

## 수동 이벤트 추가

```bash
python3 -m index_sniper.event_risk_guard_pro --add-event "Trump says no talks with Iran" --body "policy flip risk" --ts "2026-07-09T06:48:00Z"
```

## PRO 기능 요약

```text
이벤트 수집
→ 중복 제거
→ risk/volatility/uncertainty/sentiment 점수화
→ BTCUSDT 이벤트 발생가 저장
→ 5m/15m/1h/4h/24h 반응률 자동 라벨링
→ guard action 생성
→ CSV로 검증 데이터 내보내기
```
MD

chmod +x run_v61_event_guard_pro_once.sh run_v61_event_guard_pro_report.sh run_v61_event_guard_pro_doctor.sh export_v61_event_guard_pro_csv.sh start_v61_event_guard_pro.sh stop_v61_event_guard_pro.sh

if [ -f .env.example ]; then
  if ! grep -q "EVENT_RISK_PRO" .env.example; then
    cat >> .env.example <<'ENV'

# v6.1 PRO Event Risk Guard
EVENT_RISK_PRO_CONFIG="config/event_risk_guard_pro.json"
CRYPTOPANIC_TOKEN=""
CRYPTOPANIC_PLAN="developer"
CRYPTOPANIC_CURRENCIES="BTC,ETH,SOL,XRP,TAO,HYPE,AAVE,USDT,USDC"
EVENT_RISK_INTERVAL_SEC="300"
EVENT_RISK_LOOKBACK_HOURS="6"
EVENT_RISK_GDELT_TIMESPAN="1d"
EVENT_RISK_GDELT_MAXRECORDS="75"
EVENT_RISK_RSS_URLS=""
ENV
  fi
else
cat > .env.example <<'ENV'
# v6.1 PRO Event Risk Guard
EVENT_RISK_PRO_CONFIG="config/event_risk_guard_pro.json"
CRYPTOPANIC_TOKEN=""
CRYPTOPANIC_PLAN="developer"
CRYPTOPANIC_CURRENCIES="BTC,ETH,SOL,XRP,TAO,HYPE,AAVE,USDT,USDC"
EVENT_RISK_INTERVAL_SEC="300"
EVENT_RISK_LOOKBACK_HOURS="6"
EVENT_RISK_GDELT_TIMESPAN="1d"
EVENT_RISK_GDELT_MAXRECORDS="75"
EVENT_RISK_RSS_URLS=""
ENV
fi

python3 -m py_compile index_sniper/event_risk_guard_pro.py

echo ""
echo "✅ BTC Quant v6.1 PRO Event Risk Guard 적용 완료"
echo ""
echo "1회 수집/라벨링:"
echo "  bash run_v61_event_guard_pro_once.sh"
echo ""
echo "계속 수집 시작:"
echo "  bash start_v61_event_guard_pro.sh"
echo ""
echo "상태 리포트:"
echo "  bash run_v61_event_guard_pro_report.sh"
echo ""
echo "Provider 점검:"
echo "  bash run_v61_event_guard_pro_doctor.sh"
echo ""
echo "CSV 내보내기:"
echo "  bash export_v61_event_guard_pro_csv.sh"
echo ""
echo "데이터: data/event_risk_pro/events.sqlite3 / data/event_risk_pro/events.jsonl"
