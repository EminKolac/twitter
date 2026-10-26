"""ROLE 6 — Auditor. journal.csv is written by gate/paper (entries, exits, fees, slippage, skips).
weekly() builds reports/weekly_<ISOyear>-W<week>.md against LOCKED thresholds. Never changes thresholds."""
from datetime import datetime, timezone
from .store import THRESHOLDS, CFG, rows, REPORTS, f


def _max_dd(values):
    peak, mdd = None, 0.0
    for v in values:
        peak = v if peak is None else max(peak, v)
        mdd = max(mdd, (peak - v) / peak * 100 if peak else 0)
    return mdd


def stats():
    closed = [p for p in rows("positions") if p["status"] == "CLOSED"]
    rs = [f(p["r_multiple"]) for p in closed]
    n = len(rs)
    # a win must net >= WIN_MIN_R: breakeven scratches (+0.0000..0.0004R after rounding) are not wins.
    # Strict subset of r > 0, so this can only LOWER the win rate vs the old count; the locked 33% is unchanged.
    wins = sum(r >= CFG["WIN_MIN_R"] for r in rs)
    wins_legacy = sum(r > 0 for r in rs)
    bal = CFG["START_BALANCE"]
    path = [bal]
    for j in rows("journal"):
        bal += f(j["amount_usdt"])
        path.append(bal)
    dd_realized = _max_dd(path)
    # mark-to-market equity sampled at every run (open P&L included); gate on the worse of the two
    dd_mtm = _max_dd([CFG["START_BALANCE"]] + [f(e["equity"]) for e in rows("equity")])
    mdd = max(dd_realized, dd_mtm)
    j = rows("journal")
    base = [f(p["r_multiple"]) for p in closed if int(f(p["add_number"])) == 0]
    adds = [f(p["r_multiple"]) for p in closed if int(f(p["add_number"])) > 0]
    by_exit = {}
    for p in closed:
        by_exit.setdefault(p["exit_reason"], []).append(f(p["r_multiple"]))
    risk = sum(f(p["risk_pct"]) for p in closed)
    diag = {"win_rate_legacy_r_gt_0": wins_legacy / n if n else 0.0,
            "tp_rate": sum(p["exit_reason"] == "TP" for p in closed) / n if n else 0.0,
            "be_scratches": sum(p["exit_reason"] == "BE_STOP" for p in closed),
            "expectancy_base_r": sum(base) / len(base) if base else 0.0, "n_base": len(base),
            "expectancy_adds_r": sum(adds) / len(adds) if adds else 0.0, "n_adds": len(adds),
            # R weighted by risk taken (an add at 0.5% counts half), the stricter view when adds lose
            "expectancy_risk_weighted_r": sum(f(p["r_multiple"]) * f(p["risk_pct"]) for p in closed) / risk
            if risk else 0.0,
            "mean_r_by_exit": {k: round(sum(v) / len(v), 4) for k, v in sorted(by_exit.items())},
            "max_dd_realized_pct": dd_realized, "max_dd_mtm_pct": dd_mtm}
    return {"trades": n, "wins": wins, "win_rate": wins / n if n else 0.0, "expectancy_r": sum(rs) / n if n else 0.0,
            "max_dd_pct": mdd, "balance": bal, "net_pnl": bal - CFG["START_BALANCE"], "diag": diag,
            "fees": -sum(f(x["amount_usdt"]) for x in j if x["event"] == "FEE"),
            "slippage": -sum(f(x["amount_usdt"]) for x in j if x["event"] == "SLIPPAGE"),
            "skipped": sum(x["event"] == "SKIP" for x in j),
            "open": sum(p["status"] == "OPEN" for p in rows("positions")), "closed_list": closed}


def weekly():
    s = stats()
    T = THRESHOLDS
    checks = {
        f"trades >= {T['min_trades']}": s["trades"] >= T["min_trades"],
        f"win rate (R >= +{CFG['WIN_MIN_R']}) >= {T['min_win_rate']:.0%}": s["win_rate"] >= T["min_win_rate"],
        f"expectancy >= +{T['min_expectancy_r']}R": s["expectancy_r"] >= T["min_expectancy_r"],
        f"max DD <= {T['max_dd_pct']:.0f}%": s["max_dd_pct"] <= T["max_dd_pct"],
    }
    if s["trades"] < T["min_trades"]:
        verdict = "INSUFFICIENT SAMPLE — keep paper trading, no conclusions yet"
    elif all(checks.values()):
        verdict = "PASS — all locked thresholds met (still paper; going live is a human decision)"
    else:
        verdict = "FAIL — at least one locked threshold missed"
    now = datetime.now(timezone.utc)
    y, w, _ = now.isocalendar()
    path = REPORTS / f"weekly_{y}-W{w:02d}.md"
    lines = [f"# Weekly audit — {y}-W{w:02d} (generated {now:%Y-%m-%d %H:%M} UTC)", "",
             "Paper desk only. Thresholds are LOCKED (see DESK_RULES.md) and are never modified by the auditor.", "",
             "| Metric | Value | Threshold | OK |", "|---|---|---|---|",
             f"| Closed trades | {s['trades']} | >= {T['min_trades']} | {'✅' if s['trades'] >= T['min_trades'] else '❌'} |",
             f"| Win rate (R >= +{CFG['WIN_MIN_R']}R) | {s['win_rate']:.1%} | >= {T['min_win_rate']:.0%} | {'✅' if checks[list(checks)[1]] else '❌'} |",
             f"| Expectancy | {s['expectancy_r']:+.3f}R | >= +{T['min_expectancy_r']}R | {'✅' if checks[list(checks)[2]] else '❌'} |",
             f"| Max drawdown (worse of realized / MTM) | {s['max_dd_pct']:.2f}% | <= {T['max_dd_pct']:.0f}% | {'✅' if checks[list(checks)[3]] else '❌'} |",
             "", f"**Verdict:** {verdict}", "",
             f"- Paper balance: {s['balance']:.2f} USDT (start {CFG['START_BALANCE']:.0f}), net {s['net_pnl']:+.2f}",
             f"- Fees paid: {s['fees']:.2f} USDT, slippage: {s['slippage']:.2f} USDT",
             f"- Skipped alerts (risk rejections): {s['skipped']}", f"- Open positions: {s['open']}", "",
             "## Diagnostics (context only, not gated)", "",
             f"- Old win rate (any R > 0, counts breakeven scratches): {s['diag']['win_rate_legacy_r_gt_0']:.1%}; "
             f"TP-only: {s['diag']['tp_rate']:.1%}; BE scratches: {s['diag']['be_scratches']}",
             f"- Expectancy initial entries: {s['diag']['expectancy_base_r']:+.3f}R (n={s['diag']['n_base']}); "
             f"adds: {s['diag']['expectancy_adds_r']:+.3f}R (n={s['diag']['n_adds']}); "
             f"risk-weighted: {s['diag']['expectancy_risk_weighted_r']:+.3f}R",
             f"- Mean R by exit: {s['diag']['mean_r_by_exit']}",
             f"- Max DD realized {s['diag']['max_dd_realized_pct']:.2f}% / mark-to-market {s['diag']['max_dd_mtm_pct']:.2f}%", "",
             "## Closed trades", "", "| trade | coin | add | opened | closed | reason | net USDT | R |", "|---|---|---|---|---|---|---|---|"]
    for p in s["closed_list"]:
        lines.append(f"| {p['trade_id']} | {p['coin']} | {p['add_number']} | {p['opened_at']} | {p['closed_at']} | "
                     f"{p['exit_reason']} | {f(p['pnl_net']):+.2f} | {f(p['r_multiple']):+.2f} |")
    path.write_text("\n".join(lines) + "\n")
    out = {k: v for k, v in s.items() if k != "closed_list"}
    out.update({"checks": checks, "verdict": verdict, "report": str(path), "thresholds_locked": T})
    return out
