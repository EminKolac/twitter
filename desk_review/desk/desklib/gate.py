"""ROLE 4 — Gate. No Telegram: every new APPROVED plan becomes a human-readable block in alerts/pending_alerts.md
plus a chart PNG. Paper mode auto-records decision TAKEN (decisions.csv). REJECTED plans are recorded as SKIPPED and
journaled by the auditor. Execution: NEVER — nothing here can place an order."""
from . import charts
from .store import rows, append, alert, iso, now_ms, f, fmt, journal, CFG


def run(client):
    done = {d["plan_id"] for d in rows("decisions")}
    zones = rows("zones")
    wl = {w["coin"]: w for w in rows("watchlist")}
    decs, new_alerts, skipped = [], [], []
    for p in rows("trade_plan"):
        if p["plan_id"] in done:
            continue
        if p["status"] == "REJECTED":
            decs.append({"plan_id": p["plan_id"], "coin": p["coin"], "alert_time": iso(now_ms()), "decision": "SKIPPED",
                         "mode": "PAPER", "chart": "", "note": p["reason"]})
            journal([{"ts": iso(now_ms()), "trade_id": p["plan_id"], "coin": p["coin"], "event": "SKIP", "side": "SHORT",
                      "price": p["entry"], "qty": 0, "notional": 0, "amount_usdt": 0, "r_multiple": "",
                      "note": f"risk officer rejected: {p['reason']}"}])
            skipped.append({"plan_id": p["plan_id"], "reason": p["reason"]})
            continue
        z = next((z for z in zones if z["zone_id"] == p["zone_id"]), {})
        p2 = dict(p, anchor=z.get("anchor_high") or wl.get(p["coin"], {}).get("anchor_high"))
        try:
            chart = charts.plot(client, p2, zones, p["plan_id"])
        except Exception as e:
            chart = f"(chart failed: {e})"
        sig = next((s for s in rows("signals") if s["signal_id"] == p["signal_id"]), {})
        e, s, tp = f(p["entry"]), f(p["stop"]), f(p["tp"])
        tag = wl.get(p["coin"], {}).get("tag", "-")
        alert(f"""## 🔔 [PAPER] SHORT ALERT — {p['coin']} — {sig.get('candle_time', '')} 1H close
- **Plan ID:** `{p['plan_id']}`   | watchlist tag: {tag} | add #{p['add_number']}
- **Zone (4H bearish FVG, ladder {z.get('ladder', '?')}):** {fmt(z.get('zone_bottom', 0))} – {fmt(z.get('zone_top', 0))} (status {z.get('status', '?')})
- **Confirmation:** 1H {sig.get('kind', '')} (H {fmt(sig.get('high', 0))} / C {fmt(sig.get('close', 0))})
- **Entry (1H close):** {fmt(e)}
- **Stop:** {fmt(s)}  (+{(s - e) / e:.2%} above entry)
- **Take-profit:** {fmt(tp)} (impulse low)  → RR {f(p['rr']):.2f}
- **Size:** {f(p['size']):.6g} {p['coin'][:-4]}  | notional {f(p['notional']):.2f} USDT | implied leverage {f(p['implied_leverage']):.2f}x
- **Risk:** {f(p['risk_pct']):.2f}% = {f(p['risk_usd']):.2f} USDT (incl. est. fees+slippage) {('| ' + p['reason']) if p['reason'] else ''}
- **Invalidation:** any 1D close above anchor {fmt(p2['anchor'] or 0)} → EXIT NOW
- **Chart:** {chart}
- **Decision:** TAKEN automatically for PAPER simulation. ⚠️ NO real order placed — execution: NEVER.""")
        decs.append({"plan_id": p["plan_id"], "coin": p["coin"], "alert_time": iso(now_ms()), "decision": "TAKEN",
                     "mode": "PAPER", "chart": chart, "note": ""})
        new_alerts.append({"plan_id": p["plan_id"], "coin": p["coin"], "entry": e, "stop": s, "tp": tp,
                           "size": f(p["size"]), "notional": f(p["notional"]), "leverage": f(p["implied_leverage"]),
                           "risk_pct": f(p["risk_pct"]), "chart": chart})
    append("decisions", decs)
    return {"new_alerts": new_alerts, "skipped": skipped}
