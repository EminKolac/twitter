"""ROLE 3 — Risk officer. Turns unprocessed signals into trade_plan.csv rows (APPROVED or REJECTED + reason)."""
from datetime import datetime, timezone
from .store import CFG, H1, D1, rows, save, append, iso, now_ms, f, balance
from .rules import atr


def slip_bps(coin):
    r = next((w for w in rows("watchlist") if w["coin"] == coin), None)
    rank = int(f(r["volume_rank"], 999)) if r else 999
    return CFG["SLIP_BPS_TOP20"] if rank <= 20 else CFG["SLIP_BPS_OTHER"]


def btc_impulse_x(client, h_ms):
    """(BTC close[h] - close[h-3]) / ATR14(1H) on candle h, using closes up to h only. None if unavailable."""
    if getattr(client, "_btc_unavailable", False):  # one failed fetch per run is enough
        return None
    try:
        d = client.klines("BTCUSDT", "1h", 60)
    except Exception:
        client._btc_unavailable = True
        return None
    try:
        i = int(d.index[d.open_time == h_ms][0])
        if i < 17:
            return None
        a = atr(d.iloc[i - 15:i + 1])
        return float(d.close.iloc[i] - d.close.iloc[i - 3]) / a if a > 0 else None
    except Exception:
        return None


def flip_age_days(w, h_ms):
    try:
        flip_ms = datetime.strptime(w["flip_date"], "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000
        return (h_ms - flip_ms) / D1
    except (KeyError, TypeError, ValueError):
        return None


def run(client=None, latest_1h=None):
    """latest_1h: open time of the newest closed 1H candle in this run; older (catch-up) signals are not traded."""
    sigs = rows("signals")
    plans = []
    bal = balance()
    allpos = rows("positions")
    pos = [p for p in allpos if p["status"] == "OPEN"]
    wl = {w["coin"]: w for w in rows("watchlist")}
    # priority when several confirm on the same candle: earlier candle, PRIME tag, then higher 24h volume
    todo = sorted([s for s in sigs if not s["processed"]],
                  key=lambda s: (int(f(s["candle_open_ms"])), wl.get(s["coin"], {}).get("tag") != "PRIME",
                                 int(f(wl.get(s["coin"], {}).get("volume_rank"), 999))))
    cluster = {}
    for s in todo:
        cluster[int(f(s["candle_open_ms"]))] = cluster.get(int(f(s["candle_open_ms"])), 0) + 1
    new_on_candle = {}
    for s in todo:
        s["processed"] = iso(now_ms())
        coin = s["coin"]
        c_ms = int(f(s["candle_open_ms"]))
        entry, stop, tp = f(s["close"]), f(s["stop_level"]), f(s["impulse_low"])
        open_coin = sorted([p for p in pos if p["coin"] == coin], key=lambda p: int(f(p["add_number"])))
        is_add = bool(open_coin)
        # legs counted against MAX_ADDS: every open leg on the coin (incl. approvals earlier in this batch), plus
        # closed legs of the same flip (anchor within FLIP_ANCHOR_TOL: data sources differ by a tick)
        anchor = f(s["anchor_high"]) or (f(open_coin[0].get("anchor_high")) if open_coin else 0.0)
        legs = [p for p in allpos + [q for q in pos if "trade_id" not in q]
                if p["coin"] == coin and (p.get("status", "OPEN") == "OPEN" or (
                    anchor and abs(f(p.get("anchor_high")) - anchor) <= CFG["FLIP_ANCHOR_TOL"] * anchor))]
        adds_done = sum(int(f(p["add_number"])) > 0 for p in legs)
        add_no = max([int(f(p["add_number"])) for p in legs] + [0]) + 1 if is_add else 0
        risk_pct = CFG["RISK_PCT"] if not is_add else CFG["ADD_RISK_PCT"]
        plan = {"plan_id": f"P-{s['signal_id']}", "signal_id": s["signal_id"], "created_at": iso(now_ms()),
                "coin": coin, "zone_id": s["zone_id"], "entry": entry, "stop": round(stop, 10), "tp": tp,
                "add_number": add_no, "risk_pct": risk_pct, "kind": s.get("kind", "")}
        reason = ""
        dist = stop - entry
        rr = (entry - tp) / dist if dist > 0 else 0
        # decision-time features (logged on every plan so filters can be tested on the journal later)
        sb = slip_bps(coin) / 1e4
        cost = (CFG["TAKER_FEE"] + sb) * (entry + stop)  # round-trip costs per unit, as in sizing
        cost_share = cost / (dist + cost) if dist > 0 else 1.0
        rr_net = (entry - tp - (CFG["TAKER_FEE"] + sb) * (entry + tp)) / (dist + cost) if dist > 0 else 0
        late_h = (latest_1h - c_ms) / H1 if latest_1h is not None else 0.0
        age = flip_age_days(wl.get(coin, {}), c_ms)
        btc_x = (btc_impulse_x(client, c_ms) if client is not None and not is_add
                 and (late_h <= 0 or not CFG["TRADE_LATEST_HOUR_ONLY"]) else None)
        open_risk = sum(f(p.get("risk_pct")) for p in pos if p.get("be_moved") != "True")
        plan.update({"rr_net": round(rr_net, 3), "cost_share": round(cost_share, 4),
                     "stop_atr": round(dist / f(s["atr_1h"]), 3) if f(s["atr_1h"]) > 0 else "",
                     "cluster_n": cluster[c_ms], "flip_age_d": round(age, 2) if age is not None else "",
                     "btc_x": round(btc_x, 3) if btc_x is not None else "", "late_h": round(late_h, 2)})
        # quality checks first, capacity checks last (so rejection reasons can be analysed)
        if CFG["TRADE_LATEST_HOUR_ONLY"] and late_h > 0:
            reason = f"late signal ({late_h:.0f}h old, catch-up): zones/TP were mapped with later candles"
        elif bal <= 0:
            reason = "paper balance <= 0"
        elif coin not in wl:
            reason = "coin not on current watchlist (no new entries/adds)"
        elif dist <= 0:
            reason = "stop not above entry"
        elif dist / entry < CFG["MIN_STOP_PCT"]:
            reason = f"stop too tight ({dist / entry:.3%} < {CFG['MIN_STOP_PCT']:.2%}, fee-dominated)"
        elif dist / entry > CFG["MAX_STOP_PCT"]:
            reason = f"stop too wide ({dist / entry:.2%} > {CFG['MAX_STOP_PCT']:.0%})"
        elif CFG["MAX_COST_SHARE"] and cost_share > CFG["MAX_COST_SHARE"]:
            reason = f"costs are {cost_share:.0%} of 1R > {CFG['MAX_COST_SHARE']:.0%} (fee-dominated)"
        elif rr < CFG["MIN_RR"]:
            reason = f"RR to TP {rr:.2f} < {CFG['MIN_RR']}"
        elif CFG["MIN_RR_NET"] and rr_net < CFG["MIN_RR_NET"]:
            reason = f"RR to TP net of costs {rr_net:.2f} < {CFG['MIN_RR_NET']}"
        elif CFG["MAX_FLIP_AGE_DAYS"] and not is_add and age is not None and age > CFG["MAX_FLIP_AGE_DAYS"]:
            reason = f"flip is {age:.1f} days old > {CFG['MAX_FLIP_AGE_DAYS']}"
        elif is_add and open_coin[-1]["be_moved"] != "True":
            reason = "previous entry on coin not at breakeven (add blocked)"
        elif is_add and adds_done >= CFG["MAX_ADDS"]:
            reason = f"max adds ({CFG['MAX_ADDS']}) already used on this flip"
        elif not is_add and len({p['coin'] for p in pos}) >= CFG["MAX_OPEN_COINS"]:
            reason = f"max open coins ({CFG['MAX_OPEN_COINS']}) reached"
        elif not is_add and new_on_candle.get(c_ms, 0) >= CFG["MAX_NEW_PER_CANDLE"]:
            reason = f"per-candle cap: {CFG['MAX_NEW_PER_CANDLE']} new coins already approved on this 1H candle"
        elif open_risk + risk_pct > CFG["OPEN_RISK_BUDGET_PCT"] + 1e-9:
            reason = f"open risk budget: {open_risk:.2f}% not at BE + {risk_pct}% > {CFG['OPEN_RISK_BUDGET_PCT']}%"
        elif (not is_add and CFG["BTC_IMPULSE_ATR"] and btc_x is not None
              and btc_x > CFG["BTC_IMPULSE_ATR"]):
            reason = f"BTC 3h impulse {btc_x:.2f}x ATR > {CFG['BTC_IMPULSE_ATR']} (correlated squeeze risk)"
        size = notional = risk_usd = lev = 0.0
        if not reason:
            per_unit = dist + cost  # loss per unit at stop incl. costs
            risk_usd = bal * risk_pct / 100
            size = risk_usd / per_unit
            notional = size * entry
            notes = []
            if notional > CFG["MAX_LEVERAGE"] * bal:
                size = CFG["MAX_LEVERAGE"] * bal / entry
                notes.append(f"size capped at {CFG['MAX_LEVERAGE']}x")
            gross_open = sum(f(p["notional"]) for p in pos)
            room = CFG["MAX_GROSS_LEVERAGE"] * bal - gross_open
            if size * entry > room:
                if room <= 0.2 * size * entry:
                    reason = "gross leverage cap reached"
                size = max(room, 0) / entry
                notes.append("size reduced to gross leverage cap")
            notional = size * entry
            risk_usd = size * per_unit
            lev = notional / bal
            plan["reason"] = "; ".join(notes)
        plan.update({"size": round(size, 8), "notional": round(notional, 4), "risk_usd": round(risk_usd, 4),
                     "risk_pct": round(risk_usd / bal * 100, 4) if bal > 0 and not reason else risk_pct,
                     "implied_leverage": round(lev, 3), "rr": round(rr, 3),
                     "status": "REJECTED" if reason else "APPROVED"})
        if reason:
            plan["reason"] = reason
        else:  # count this approval against coin/adds/gross/risk caps for the rest of the batch
            pos.append({"coin": coin, "add_number": add_no, "be_moved": "False", "notional": plan["notional"],
                        "risk_pct": plan["risk_pct"], "anchor_high": s["anchor_high"]})
            if not is_add:
                new_on_candle[c_ms] = new_on_candle.get(c_ms, 0) + 1
        plans.append(plan)
    save("signals", sigs)
    append("trade_plan", plans)
    return {"approved": [p["plan_id"] for p in plans if p["status"] == "APPROVED"],
            "rejected": [{"plan_id": p["plan_id"], "reason": p["reason"]} for p in plans if p["status"] == "REJECTED"]}
