#!/usr/bin/env python3
"""Read-only analysis of the paper desk's own CSV log (no network). Answers, from the 147 trades you already have:
  1. the honest scorecard (win rate without breakeven scratches, confidence intervals, clustered by hour),
  2. which splits carry the expectancy drag (adds, stop width / cost share, RR, kind, same-candle clusters),
  3. how many trades are contaminated by the bugs found in the review (catch-up lookahead, stale-state adds,
     re-traded zones, add-cap leak, entries on coins that were not on the watchlist).

usage:  python3 analyze_log.py /path/to/desk            (the folder that holds data/ and logs/)
Writes nothing. Pre-registered decision rules are printed next to each split; read them before the numbers."""
import json, math, sys
from pathlib import Path
import pandas as pd

TAKER_FEE = 0.0005
MAX_ADDS = 2
BOOT = 4000


def load(root):
    d = root / "data"
    rd = lambda n: pd.read_csv(d / f"{n}.csv", dtype=str).fillna("") if (d / f"{n}.csv").exists() else pd.DataFrame()
    pos, sig, plan, dec, jr = rd("positions"), rd("signals"), rd("trade_plan"), rd("decisions"), rd("journal")
    runs = []
    rl = root / "logs" / "runs.jsonl"
    if rl.exists():
        for line in rl.read_text().splitlines():
            try:
                runs.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return pos, sig, plan, dec, jr, runs


def num(s, col):
    return pd.to_numeric(s[col], errors="coerce")


def wilson(k, n, z=1.96):
    if not n:
        return (0.0, 0.0)
    p = k / n
    den = 1 + z * z / n
    mid = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return (mid - half, mid + half)


def boot_ci(r, groups=None, seed=7):
    """95% bootstrap CI of the mean; with groups, resample whole groups (clustered by hour)."""
    import random
    rnd = random.Random(seed)
    r = list(r)
    if not r:
        return (float("nan"), float("nan"))
    if groups is None:
        units = [[x] for x in r]
    else:
        g = {}
        for x, k in zip(r, groups):
            g.setdefault(k, []).append(x)
        units = list(g.values())
    means = []
    for _ in range(BOOT):
        pick = [units[rnd.randrange(len(units))] for _ in units]
        flat = [x for u in pick for x in u]
        means.append(sum(flat) / len(flat))
    means.sort()
    return (means[int(0.025 * BOOT)], means[int(0.975 * BOOT)])


def split(df, key, title, rule=""):
    print(f"\n### {title}")
    if rule:
        print(f"    decision rule: {rule}")
    g = df.groupby([key, "half"], dropna=False, observed=False)  # each bucket per half of the log
    out = g.agg(n=("R", "size"), meanR=("R", "mean"), TP=("exit_reason", lambda x: (x == "TP").mean()),
                STOP=("exit_reason", lambda x: (x == "STOP").mean()),
                BE=("exit_reason", lambda x: (x == "BE_STOP").mean()))
    print(out.round(3).to_string())


def main(root):
    pos, sig, plan, dec, jr, runs = load(root)
    if pos.empty:
        sys.exit("no data/positions.csv under " + str(root))
    c = pos[pos.status == "CLOSED"].copy()
    c["R"] = num(c, "r_multiple")
    for col in ("entry", "initial_stop", "tp", "slip_bps", "add_number", "opened_ms", "risk_pct"):
        c[col] = num(c, col)
    c["hour"] = (c.opened_ms // 3_600_000).astype("Int64")
    n = len(c)
    print(f"# Paper desk log analysis — {root}\n\nclosed trades: {n}")
    if not n:
        return
    c["half"] = (c.opened_ms.rank(method="first") > n // 2).map({False: "H1", True: "H2"})
    mix = c.exit_reason.value_counts().to_dict()
    print("exit mix:", mix)

    # ---------- 1. honest scorecard ----------
    E = c.R.mean()
    lo, hi = boot_ci(c.R)
    clo, chi = boot_ci(c.R, c.hour.tolist())
    wins_legacy = int((c.R > 0).sum())
    wins = int((c.R >= 0.10).sum())
    w_lo, w_hi = wilson(wins, n)
    p1 = ((c.exit_reason == "TP") | (c.exit_reason == "BE_STOP")).mean()
    print(f"\n## 1. Scorecard\nexpectancy {E:+.3f}R | 95% CI iid [{lo:+.3f}, {hi:+.3f}] | clustered by entry hour "
          f"[{clo:+.3f}, {chi:+.3f}]")
    print(f"win rate old (R > 0): {wins_legacy / n:.1%}  |  honest (R >= +0.10): {wins / n:.1%} "
          f"[Wilson {w_lo:.1%}, {w_hi:.1%}]  |  locked gate 33%")
    be = c[c.exit_reason == "BE_STOP"]
    print(f"BE_STOP rows logged with R > 0 (counted as wins by the old auditor): {(be.R > 0).sum()} of {len(be)}")
    print(f"share reaching +1R before -1R (TP or BE): {p1:.1%};  P(TP | +1R) = "
          f"{(c.exit_reason == 'TP').sum() / max(1, ((c.exit_reason == 'TP') | (c.exit_reason == 'BE_STOP')).sum()):.1%}")
    tp = c[c.exit_reason == "TP"]
    if len(tp):
        print(f"mean TP payoff {tp.R.mean():+.2f}R  (identity: E = p1 x E[R | +1R] - (1 - p1))")

    # ---------- 2. splits ----------
    c["stop_pct"] = (c.initial_stop - c.entry) / c.entry
    cs = (TAKER_FEE + c.slip_bps / 1e4)
    c["cost_share"] = cs * (c.entry + c.initial_stop) / ((c.initial_stop - c.entry) + cs * (c.entry + c.initial_stop))
    c["rr_gross"] = (c.entry - c.tp) / (c.initial_stop - c.entry)
    c["rr_net"] = (c.entry - c.tp - cs * (c.entry + c.tp)) / ((c.initial_stop - c.entry) + cs * (c.entry + c.initial_stop))
    c["leg"] = c.add_number.map(lambda a: "initial" if a == 0 else "add")
    print("\n## 2. Splits (in-sample: treat as hypotheses; adopt a filter only if it holds in BOTH halves of the log)")
    split(c, "leg", "initial entries vs adds",
          "if adds' mean R < initial entries' mean R -> MAX_ADDS = 0 (exact counterfactual = initial-only mean R)")
    c["stop_bucket"] = pd.cut(c.stop_pct, [0, .004, .006, .01, .02, 1], labels=["<0.4%", "0.4-0.6%", "0.6-1%", "1-2%", ">2%"])
    split(c, "stop_bucket", "stop distance",
          "tight buckets with mean R below the overall mean AND a higher BE share -> test MAX_COST_SHARE")
    c["cost_bucket"] = pd.cut(c.cost_share, [0, .10, .15, .20, .30, 1], labels=["<10%", "10-15%", "15-20%", "20-30%", ">30%"])
    split(c, "cost_bucket", "round-trip cost as share of 1R")
    c["rr_bucket"] = pd.cut(c.rr_gross, [0, 1.5, 2.5, 4, 1e9], labels=["1-1.5", "1.5-2.5", "2.5-4", ">4"])
    split(c, "rr_bucket", "gross RR to TP")
    c["rr_net_lt1"] = c.rr_net < 1
    split(c, "rr_net_lt1", "RR net of costs < 1", "mean R of the True row below overall -> MIN_RR_NET = 1.0")
    if not sig.empty:
        k = sig.set_index("signal_id").kind.to_dict()
        c["kind"] = c.trade_id.str[2:].map(k)
        split(c, "kind", "confirmation kind (REJECT vs BEARISH_INSIDE)")
    ini = c[c.add_number == 0].copy()
    ini["cluster"] = ini.groupby("opened_ms").trade_id.transform("size").clip(upper=3).map({1: "1", 2: "2", 3: "3+"})
    split(ini, "cluster", "initial entries by number of coins opened on the same 1H candle",
          "co-stops / DD matter more than the mean here: compare STOP share and see the co-stop rate below")
    multi = ini[ini.groupby("opened_ms").trade_id.transform("size") >= 2]
    if len(multi):
        co = multi.groupby("opened_ms").apply(lambda g: (g.exit_reason == "STOP").sum() >= 2).mean()
        print(f"    same-candle clusters where >= 2 legs hit the full STOP: {co:.0%} of {multi.opened_ms.nunique()} clusters")
    half = c.sort_values("opened_ms")
    h1, h2 = half.iloc[: n // 2], half.iloc[n // 2:]
    print(f"\nfirst half E {h1.R.mean():+.3f}R (n={len(h1)}) | second half E {h2.R.mean():+.3f}R (n={len(h2)})")

    # ---------- 3. contamination from bugs found in review ----------
    print("\n## 3. Trades affected by bugs (exclude them, then re-read section 1)")
    flagged = {}
    if not dec.empty and not sig.empty:
        st = sig.set_index("signal_id").candle_open_ms.to_dict()
        d2 = dec[dec.decision == "TAKEN"].copy()
        d2["sig"] = d2.plan_id.str[2:]
        d2["candle_close"] = d2.sig.map(lambda s: int(float(st[s])) + 3_600_000 if s in st else None)
        ts = pd.to_datetime(d2.alert_time.str.replace(" UTC", ""), utc=True, errors="coerce")
        d2["alert_ms"] = (ts - pd.Timestamp(0, tz="UTC")) // pd.Timedelta(milliseconds=1)  # unit-safe (pandas 2 and 3)
        late = d2[(d2.alert_ms - d2.candle_close) > 3_600_000].plan_id
        flagged["catch-up lookahead (alert > 1h after the candle closed)"] = set(late)
    adds = c[c.add_number > 0]
    stale = set()
    for _, a in adds.iterrows():
        base = c[(c.coin == a.coin) & (c.add_number < a.add_number) & (c.closed_at == a.opened_at)]
        if len(base):
            stale.add(a.trade_id)
    # same run: an add sized on the newest candle against a base that the run's (late) walk closed earlier
    cl_ms = dict(zip(c.trade_id, (pd.to_datetime(c.closed_at.str.replace(" UTC", ""), utc=True, errors="coerce")
                                  - pd.Timestamp(0, tz="UTC")) // pd.Timedelta(milliseconds=1)))
    for r in runs:
        gone = {e.get("trade_id") for e in r.get("exits", [])}
        for al in r.get("new_alerts", []):
            a = adds[adds.trade_id == al.get("plan_id")]
            if not len(a):
                continue
            a = a.iloc[0]
            if any(b.coin == a.coin and b.add_number < a.add_number and b.opened_ms < a.opened_ms
                   and cl_ms.get(b.trade_id, float("inf")) <= a.opened_ms
                   for b in c[c.trade_id.isin(gone)].itertuples()):
                stale.add(a.trade_id)
    flagged["stale-state add (its base closed before the add was sized)"] = stale
    if not sig.empty:
        dup = sig.groupby("zone_id").signal_id.apply(list)
        retr = {"P-" + s for ids in dup[dup.map(len) > 1] for s in ids[1:]}
        flagged["zone traded again after re-creation"] = retr & set(c.trade_id)
    leak = set()
    allp = pos.assign(add_number=num(pos, "add_number"), opened_ms=num(pos, "opened_ms"))
    for (coin, anc), g in allp.groupby(["coin", "anchor_high"]):  # open AND closed adds of the flip
        a = g[g.add_number > 0].sort_values("opened_ms")
        leak |= set(a.trade_id.iloc[MAX_ADDS:])
    flagged["add-cap leak (> 2 adds on one flip)"] = leak
    if runs:
        off = set()
        for r in runs:
            wlc = {w["coin"] for w in r.get("watchlist", [])}
            for a in r.get("new_alerts", []):
                if a.get("coin") not in wlc:
                    off.add(a["plan_id"])
        flagged["entered while the coin was off the watchlist"] = off & set(c.trade_id)
    allf = set()
    for k, v in flagged.items():
        v = v & set(c.trade_id)
        allf |= v
        sub = c[c.trade_id.isin(v)]
        print(f"- {k}: {len(v)} trades" + (f", mean R {sub.R.mean():+.3f}" if len(v) else ""))
    clean = c[~c.trade_id.isin(allf)]
    if len(clean) != n:
        cl, ch = boot_ci(clean.R, clean.hour.tolist())
        print(f"clean sample: n={len(clean)}, E {clean.R.mean():+.3f}R [clustered {cl:+.3f}, {ch:+.3f}], "
              f"honest win rate {(clean.R >= 0.10).mean():.1%}")
    else:
        print("no contaminated trades found")
    if not jr.empty:
        j = jr.copy()
        j["amt"] = pd.to_numeric(j.amount_usdt, errors="coerce").fillna(0)
        j["h"] = j.ts.str[:13]
        worst = j[j.event.isin(["EXIT", "FEE", "SLIPPAGE"])].groupby("h").amt.sum().sort_values().head(3)
        print("\nworst single hours of realised P&L (USDT):", worst.round(2).to_dict())


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "."))
