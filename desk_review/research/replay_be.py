#!/usr/bin/env python3
"""Paired exit replay on the desk's OWN closed trades, using public 1H/1D candles (no keys, read-only GET).

The decisive question from the review: after a trade reaches +1R and the stop moves to breakeven, does price keep
drifting in the short's favour (then moving BE later / not at all lifts expectancy), or is it a coin flip from there
(then exit rules cannot move expectancy and only entries can)? Your 52 BE_STOP trades answer it: replay them with the
ORIGINAL stop kept and look at their mean R.

  pre-registered rule (mean counterfactual R of the BE_STOP trades, no-BE variant):
     >= +0.25R  -> continuation after +1R is real: forward-test BE_MODE="close"/"net" or a later trigger
     <=  0.00R  -> edge decays after +1R: keep BE; exits cannot reach +0.4R, work on entries
     otherwise  -> inconclusive, collect more trades

It also re-prices every stop the candle opened through at the open (GAP_AWARE_FILLS) and reports how much of the
logged BE_STOP "0R" was really a loss.

usage:  python3 replay_be.py /path/to/desk      (reads data/positions.csv; needs internet access to Binance or Bybit)
Limits: entries are held fixed (a variant that holds longer could have blocked later entries: occupancy is not
simulated); adds are tied to BE (no BE => no adds), so results are also shown for initial entries only."""
import sys, time, random
from pathlib import Path
import pandas as pd
import requests

H1, D1 = 3_600_000, 86_400_000
TAKER_FEE = 0.0005
HORIZON_D = 30
S = requests.Session()
S.headers["User-Agent"] = "paper-desk-research/1.0 (public data only)"

VARIANTS = {  # name: (be_mode, be_units, be_trigger_r, gap_aware)
    "P0 as logged": ("wick", "price", 1.0, False),
    "P0 + gap-aware fills": ("wick", "price", 1.0, True),
    "BE on 1H close": ("close", "price", 1.0, True),
    "BE on close, net +1R": ("close", "net", 1.0, True),
    "BE wick at +1.5R": ("wick", "price", 1.5, True),
    "BE wick at +2R": ("wick", "price", 2.0, True),
    "no BE": ("none", "price", 1.0, True),
}


def _get(url, params):
    for i in range(4):
        try:
            r = S.get(url, params=params, timeout=20)
            if r.status_code == 200:
                return r.json()
        except requests.RequestException:
            pass
        time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"GET failed {url} {params}")


def klines(sym, tf, start, end):
    """Closed candles in [start, end) as a DataFrame (open_time, open, high, low, close). Binance, then Bybit."""
    rows = []
    try:
        t = start
        while t < end:
            d = _get("https://fapi.binance.com/fapi/v1/klines",
                     {"symbol": sym, "interval": tf, "startTime": t, "endTime": end - 1, "limit": 1500})
            if not d:
                break
            rows += [[int(k[0]), *map(float, k[1:5])] for k in d]
            t = int(d[-1][0]) + (H1 if tf == "1h" else D1)
            time.sleep(0.1)
    except RuntimeError:
        rows = []
    if not rows:  # Binance blocked or symbol unknown there -> Bybit (newest-first pages)
        iv = {"1h": "60", "1d": "D"}[tf]
        t = end
        while t > start:
            d = _get("https://api.bybit.com/v5/market/kline", {"category": "linear", "symbol": sym, "interval": iv,
                                                               "start": start, "end": t - 1, "limit": 1000})
            lst = d.get("result", {}).get("list", [])
            if not lst:
                break
            rows += [[int(k[0]), *map(float, k[1:5])] for k in lst]
            t = min(int(k[0]) for k in lst)
            time.sleep(0.1)
    df = pd.DataFrame(rows, columns=["open_time", "open", "high", "low", "close"]).drop_duplicates("open_time")
    step = H1 if tf == "1h" else D1
    now = int(time.time() * 1000)
    return df[(df.open_time >= start) & (df.open_time + step <= min(end, now))].sort_values("open_time").reset_index(drop=True)


def net_r(p, px):
    """paper.close() algebra: exit slippage + taker fee on the exit, entry costs already in entry_costs."""
    qty, entry, sb = p["qty"], p["entry"], p["slip_bps"] / 1e4
    fill = px * (1 + sb)
    net = (entry - px) * qty - (fill - px) * qty - fill * qty * TAKER_FEE - p["entry_costs"]
    return net / p["risk_usd"] if p["risk_usd"] else 0.0


def walk(p, h1, d1, be_mode, be_units, k, gap):
    entry, stop0, tp = p["entry"], p["initial_stop"], p["tp"]
    R = stop0 - entry
    sb = p["slip_bps"] / 1e4
    if be_units == "net":
        trig = (entry * p["qty"] - p["entry_costs"] - k * p["risk_usd"]) / (p["qty"] * (1 + sb) * (1 + TAKER_FEE))
    else:
        trig = entry - k * R
    be_px = entry * (1 - 2 * TAKER_FEE - 2 * sb)
    stop, be = stop0, False
    dclose = dict(zip(d1.open_time + D1, d1.close))
    anchor = p["anchor_high"]
    for h in h1[h1.open_time >= p["opened_ms"]].itertuples():
        if h.high >= stop:
            px = max(stop, h.open) if gap else stop
            return ("BE_STOP" if be else "STOP"), net_r(p, px), h.open_time + H1
        if h.low <= tp:
            return "TP", net_r(p, tp), h.open_time + H1
        if not be and be_mode != "none" and (h.low if be_mode == "wick" else h.close) <= trig:
            stop, be = be_px, True
        t = h.open_time + H1
        if anchor and t in dclose and dclose[t] > anchor:
            return "EXIT_NOW", net_r(p, dclose[t]), t
    last = h1.iloc[-1].close if len(h1) else entry
    return "OPEN", net_r(p, last), None


def boot_mean_ci(x, groups, seed=11, B=4000):
    rnd = random.Random(seed)
    g = {}
    for v, k in zip(x, groups):
        g.setdefault(k, []).append(v)
    units = list(g.values())
    if not units:
        return float("nan"), float("nan")
    m = []
    for _ in range(B):
        pick = [units[rnd.randrange(len(units))] for _ in units]
        flat = [v for u in pick for v in u]
        m.append(sum(flat) / len(flat))
    m.sort()
    return m[int(.025 * B)], m[int(.975 * B)]


def main(root):
    pos = pd.read_csv(root / "data" / "positions.csv", dtype=str).fillna("")
    c = pos[pos.status == "CLOSED"].copy()
    for col in ("entry", "initial_stop", "tp", "qty", "slip_bps", "entry_costs", "risk_usd", "r_multiple",
                "opened_ms", "add_number"):
        c[col] = pd.to_numeric(c[col], errors="coerce")
    c["anchor_high"] = pd.to_numeric(c.anchor_high, errors="coerce").fillna(0.0)
    c["hour"] = c.opened_ms // H1
    res = {v: [] for v in VARIANTS}
    for coin, g in c.groupby("coin"):
        start = int(g.opened_ms.min()) - H1
        end = int(g.opened_ms.max()) + HORIZON_D * D1
        try:
            h1 = klines(coin, "1h", start, end)
            d1 = klines(coin, "1d", start - D1, end)
        except Exception as e:
            print(f"skip {coin}: {e}")
            continue
        for _, p in g.iterrows():
            for v, args in VARIANTS.items():
                why, r, t = walk(p, h1, d1, *args)
                res[v].append({"trade_id": p.trade_id, "why": why, "R": r, "logged": p.exit_reason,
                               "logged_R": p.r_multiple, "hour": p.hour, "add": p.add_number})
    df = {v: pd.DataFrame(x) for v, x in res.items()}
    p0 = df["P0 as logged"]
    if p0.empty:
        sys.exit("no trades replayed (no market data?)")
    ok = ((p0.why == p0.logged) & ((p0.R - p0.logged_R).abs() < 0.02)).mean()
    print(f"# Exit replay — {len(p0)} closed trades\nreconciliation of P0 with the log: {ok:.0%} match "
          f"(< 90% means candles differ from the desk's source; read results with care)\n")
    print("| variant | E all | ΔE vs P0 [95% CI, clustered by entry hour] | E initial only | TP/STOP/BE/other | open |")
    print("|---|---|---|---|---|---|")
    for v, d in df.items():
        delta = (d.R.values - p0.R.values).tolist()
        lo, hi = boot_mean_ci(delta, p0.hour.tolist())
        ini = d[d.add == 0]
        mix = d.why.value_counts()
        print(f"| {v} | {d.R.mean():+.3f} | {sum(delta) / len(delta):+.3f} [{lo:+.3f}, {hi:+.3f}] | {ini.R.mean():+.3f} | "
              f"{mix.get('TP', 0)}/{mix.get('STOP', 0)}/{mix.get('BE_STOP', 0)}/{mix.get('EXIT_NOW', 0)} | "
              f"{mix.get('OPEN', 0)} |")
    be_ids = p0[p0.logged == "BE_STOP"].trade_id
    nobe = df["no BE"][df["no BE"].trade_id.isin(be_ids)]
    lo, hi = boot_mean_ci(nobe.R.tolist(), nobe.hour.tolist())
    m = nobe.R.mean()
    verdict = ("continuation after +1R: forward-test a later / close-based BE" if m >= 0.25 else
               "edge decays after +1R: keep BE, exits cannot reach +0.4R, work on entries" if m <= 0 else
               "inconclusive: collect more trades")
    print(f"\n## Decisive test: the {len(nobe)} logged BE_STOP trades replayed with their original stop")
    print(f"mean R {m:+.3f} [95% CI {lo:+.3f}, {hi:+.3f}] -> {verdict}")
    g = df["P0 + gap-aware fills"]
    bad = g[(g.logged == "BE_STOP") & (g.R < -0.05)]
    print(f"\nBE_STOP exits that were really losses (candle opened through the BE stop): {len(bad)}, "
          f"mean {bad.R.mean() if len(bad) else 0:+.3f}R; expectancy effect {(g.R.mean() - p0.R.mean()):+.3f}R")


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "."))
