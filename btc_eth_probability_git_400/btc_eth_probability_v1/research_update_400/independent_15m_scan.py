"""Independent, bounded 15-minute OHLCV screen; research only, no live orders.

Discovery 2023-01-01..2025-01-01, evaluation 2025-01-01..2026-09-21.
All signals use a completely closed 15-minute candle; fill one minute later
at Binance proxy minute open and exit at a fixed minute-open horizon. Per-symbol
exposures are greedily nonoverlapping. Candidate ranking never reads evaluation.
The approximate all-taker fee/spread/slippage round trip is 0.29% of notional
and 0.58% in the adverse cost scenario. Binance proxy funding is accounted for
using the nearest preceding minute open as a mark-price approximation. Partial
fills, exchange risk, stop orders, and dynamic drawdown guards are NOT covered.
"""
import hashlib
import itertools
import json
import os
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(os.environ.get("PROBABILITY_15M_CACHE", str(Path(__file__).resolve().parents[1] / "btc_eth_15m_backtest" / "cache")))
HERE = Path(__file__).resolve().parent
HERE.mkdir(exist_ok=True)
MINUTE = 60_000
DISCOVERY = (pd.Timestamp("2023-01-01", tz="UTC").value // 1_000_000,
             pd.Timestamp("2025-01-01", tz="UTC").value // 1_000_000)
EVALUATION = (DISCOVERY[1],
              pd.Timestamp("2026-09-21T15:00:00Z").value // 1_000_000)
NOTIONAL = 2_000.0  # Each symbol 400 USDT margin at 5x.


def previous_rolling(a, length, method):
    return getattr(pd.Series(a).shift(1).rolling(length, min_periods=length), method)().to_numpy()


def dataset(sym):
    with np.load(BASE / f"{sym}_features.npz") as f:
        x = f["main"]
    with np.load(BASE / f"{sym}_minute.npz") as f:
        minute = f["bars"]
    with np.load(BASE / f"{sym}_funding.npz") as f:
        funding = f["events"]
    if len(minute) != len(x)*15 or np.any(x[:,0] != minute[::15,0]):
        raise ValueError("15-minute and minute rows are misaligned")
    assert np.all(np.diff(minute[:,0]) == MINUTE)
    o,h,l,c,v = (x[:,i] for i in (1,2,3,4,5))
    volmean = previous_rolling(v,20,"mean")
    volratio = np.divide(v,volmean,out=np.full_like(v,np.nan),where=volmean>0)
    candle_range = h-l
    position = np.divide(c-l,candle_range,out=np.full_like(c,.5),where=candle_range>0)
    prevclose = np.r_[np.nan,c[:-1]]
    trend = np.full_like(c,np.nan)
    trend[96:] = (c[96:]/c[:-96])-1
    return {"symbol":sym,"ts":x[:,0].astype(np.int64),"o":o,"h":h,"l":l,"c":c,
            "v":v,"vr":volratio,"position":position,"trend":trend,
            "prevclose":prevclose,"minute":minute,"funding":funding}


def signal(data, family, lookback, volume, regime, invert):
    o,h,l,c = (data[k] for k in ("o","h","l","c"))
    prevh = previous_rolling(h,lookback,"max")
    prevl = previous_rolling(l,lookback,"min")
    if family == "breakout":
        long = (c > prevh) & (data["prevclose"] <= np.r_[np.nan,prevh[:-1]])
        short = (c < prevl) & (data["prevclose"] >= np.r_[np.nan,prevl[:-1]])
        long &= data["position"] >= .65
        short &= data["position"] <= .35
    elif family == "sweep":
        long = (l < prevl) & (c > prevl) & (data["position"] >= .60)
        short = (h > prevh) & (c < prevh) & (data["position"] <= .40)
    elif family == "pullback":
        long = (c > o) & (data["prevclose"] < np.r_[np.nan,data["prevclose"][:-1]])
        short = (c < o) & (data["prevclose"] > np.r_[np.nan,data["prevclose"][:-1]])
        long &= data["trend"] > 0
        short &= data["trend"] < 0
    elif family == "impulse":
        med = previous_rolling(h-l,lookback,"median")
        long = (c-o >= med*.9) & (data["position"] >= .75)
        short = (o-c >= med*.9) & (data["position"] <= .25)
    else:
        raise ValueError(family)
    if regime == "trend":
        long &= data["trend"] > 0
        short &= data["trend"] < 0
    if regime == "counter":
        long &= data["trend"] < 0
        short &= data["trend"] > 0
    valid = np.isfinite(data["vr"]) & (data["vr"] >= volume)
    result = np.zeros(len(c),np.int8)
    result[valid & long & ~short] = 1
    result[valid & short & ~long] = -1
    result[:max(lookback,97)] = 0
    return -result if invert else result


def trades(data, flags, duration):
    """Causal fixed-horizon proxy; no stops or drawdown guard modeled."""
    bars = np.flatnonzero(flags != 0)
    # Closed 15m candle at bar i, entry at next minute (i*15+16).
    entry = bars*15+16
    good = entry+duration < len(data["minute"])
    bars,entry = bars[good],entry[good]
    kept=[]
    next_available=0
    for b,e in zip(bars,entry):
        if e < next_available:
            continue
        kept.append(b)
        next_available=e+duration
    bars=np.array(kept,dtype=np.int64)
    entry=bars*15+16
    exit_idx=entry+duration
    x=data["minute"]
    # Entry and exit both assumed market orders. Signals never fill at own close.
    rets=flags[bars]*(x[exit_idx,1]/x[entry,1]-1)
    fund=data["funding"]
    fi=np.clip(np.searchsorted(x[:,0],fund[:,0],side="right")-1,0,len(x)-1)
    fund_value=np.r_[0,np.cumsum(fund[:,1]*x[fi,1])]
    a=np.searchsorted(fund[:,0],x[entry,0],side="right")
    b=np.searchsorted(fund[:,0],x[exit_idx,0],side="right")
    funding_fraction=-flags[bars]*(fund_value[b]-fund_value[a])/x[entry,1]
    return {"entry":x[entry,0].astype(np.int64),"exit":x[exit_idx,0].astype(np.int64),
            "gross":rets+funding_fraction,
            "symbol":np.array([data["symbol"]]*len(bars)),"direction":flags[bars]}


def stats(rows, start, end, cost):
    r=rows[(rows.entry>=start)&(rows.entry<end)&(rows.exit<=end)]
    pnl=(r.gross-cost)*NOTIONAL
    n=len(pnl)
    symbols={s:float(pnl[r.symbol==s].sum()) for s in ("BTCUSDT","ETHUSDT")}
    counts={s:int(np.count_nonzero(r.symbol==s)) for s in symbols}
    days=(end-start)/(24*60*MINUTE)
    return {"trades":n,"per_year":round(n*365.25/days,1),"total_pnl":round(float(pnl.sum()),2),
            "avg_usdt":round(float(pnl.mean()),3) if n else None,
            "breakeven_cost_bps":round(float(r.gross.mean()*10000),2) if n else None,
            "btc_pnl":round(symbols["BTCUSDT"],2),"eth_pnl":round(symbols["ETHUSDT"],2),
            "btc_trades":counts["BTCUSDT"],"eth_trades":counts["ETHUSDT"],
            "win_rate":round(float(np.mean(pnl>0)),4) if n else None}


def block_ci(rows,start,end,cost,reps=2000,block_days=7,seed=20261001):
    """Block-resampled calendar days; CI for mean daily dollar P&L, not profit probability."""
    selected=rows[(rows.entry>=start)&(rows.entry<end)&(rows.exit<=end)]
    day_ms=24*60*MINUTE
    days=int(np.ceil((end-start)/day_ms))
    idx=((selected.entry-start)//day_ms).to_numpy(dtype=np.int64)
    values=((selected.gross-cost)*NOTIONAL).to_numpy()
    daily=np.bincount(idx,weights=values,minlength=days)
    rng=np.random.default_rng(seed)
    out=np.empty(reps)
    blocks=int(np.ceil(days/block_days))
    for i in range(reps):
        starts=rng.integers(0,days,size=blocks)
        sample=(starts[:,None]+np.arange(block_days)[None,:])%days
        out[i]=daily[sample.ravel()[:days]].sum()
    return {"period_calendar_days":days,"block_days":block_days,"repetitions":reps,
            "total_pnl_95pct_interval_usdt":list(np.round(np.quantile(out,[.025,.975]),2))}


def main():
    data=[dataset(s) for s in ("BTCUSDT","ETHUSDT")]
    families = ("breakout","sweep","pullback","impulse")
    grid=[]
    # 4 x 3 x 2 x 3 x 2 x 3 = 432 fixed candidate profiles.
    for family,lookback,volume,regime,invert,duration in itertools.product(
            families,(12,24,48),(0.0,1.5),("none","trend","counter"),(False,True),(60,240,480)):
        # Pullback defines a trend already; include all nonetheless for audit.
        spec={"family":family,"lookback":lookback,"volume":volume,
              "regime":regime,"invert":invert,"minutes":duration}
        legs=[trades(d,signal(d,family,lookback,volume,regime,invert),duration) for d in data]
        record=pd.DataFrame({k:np.concatenate([leg[k] for leg in legs]) for k in legs[0]})
        dev=stats(record,*DISCOVERY,.0029)
        grid.append((spec,dev,record))
    eligible=[i for i,(sp,d,r) in enumerate(grid)
              if d["btc_trades"]>=100 and d["eth_trades"]>=100]
    ranked=sorted(eligible,key=lambda i:(grid[i][1]["total_pnl"],grid[i][1]["per_year"]),reverse=True)
    report={"method":"Fixed closed-15m signals, +1-minute market open, fixed exits, nonoverlap per symbol, no stops or drawdown guards; proxy funding at prior minute open; entry within period and exit <= end",
            "source":"Binance USD-M BTCUSDT/ETHUSDT minute OHLCV proxy, not Bitget historical fills",
            "source_coverage_utc":"2022-07-18 15:00 through 2026-09-21 14:59",
            "raw_npz_in_git":False,
            "reproduce_data_note":"Six required *_minute.npz, *_features.npz and *_funding.npz must be supplied separately in btc_eth_15m_backtest/cache; never commit the 132MB local cache to GitHub",
            "discovery_utc":["2023-01-01","2025-01-01"],
            "evaluation_utc":["2025-01-01","2026-09-21 15:00"],
            "candidate_profiles":len(grid),"structurally_impossible_pullback_counter_profiles":36,
            "active_candidate_profiles":len(grid)-36,"discovery_min100_each_symbol":len(eligible),
            "cost_rate_base":.0029,"cost_rate_double":.0058,
            "capital_per_symbol":400,"leverage":5,"notional_per_symbol":2000,
            "discovery_positive_with_min100_each":sum(grid[i][1]["total_pnl"]>0 for i in eligible),
            "source_sha256":{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(BASE.glob("*.npz"))},
            "top_discovery":[]}
    for i in ranked[:20]:
        spec,dev,record=grid[i]
        report["top_discovery"].append({"index":i,"spec":spec,"discovery":dev,
             "discovery_cost2":stats(record,*DISCOVERY,.0058),
             "evaluation":stats(record,*EVALUATION,.0029),
             "evaluation_cost2":stats(record,*EVALUATION,.0058)})
        if len(report["top_discovery"])==1:
            report["top_discovery"][-1]["discovery_calendar_block_ci"]=block_ci(record,*DISCOVERY,.0029)
            report["top_discovery"][-1]["evaluation_calendar_block_ci"]=block_ci(record,*EVALUATION,.0029)
    # For all 432 candidates the holdout is only exported for audit diagnostics;
    # winner selection above was fixed by discovery alone.
    allrows=[]
    for i,(sp,dev,record) in enumerate(grid):
        allrows.append({"index":i,"spec":json.dumps(sp,sort_keys=True),"dev_trades":dev["trades"],
                        "dev_btc":dev["btc_pnl"],"dev_eth":dev["eth_pnl"],"dev_pnl":dev["total_pnl"],
                        "eval_trades":stats(record,*EVALUATION,.0029)["trades"],
                        "eval_pnl":stats(record,*EVALUATION,.0029)["total_pnl"]})
    pd.DataFrame(allrows).to_csv(HERE/"independent_15m_all_candidates.csv",index=False)
    report["script_sha256"]=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    (HERE/"independent_15m_results.json").write_text(json.dumps(report,indent=2)+"\n")
    print(json.dumps({k:v for k,v in report.items() if k!="top_discovery"},indent=2),flush=True)
    for row in report["top_discovery"][:5]:
        print(json.dumps(row),flush=True)


if __name__=="__main__":main()
