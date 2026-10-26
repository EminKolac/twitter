"""Paper broker (simulation only). Fills at the confirmation 1H close; fees (0.05% taker per side) and slippage
are journaled on separate lines. Manages stops / TP / breakeven on closed 1H candles."""
from .store import CFG, rows, save, iso, now_ms, f, journal, alert, fmt, H1
from .risk_officer import slip_bps


def open_new():
    pos = rows("positions")
    have = {p["trade_id"] for p in pos}
    plans = {p["plan_id"]: p for p in rows("trade_plan")}
    sigs = {s["signal_id"]: s for s in rows("signals")}
    opened = []
    for d in rows("decisions"):
        if d["decision"] != "TAKEN" or d["plan_id"] in have:
            continue
        p = plans[d["plan_id"]]
        s = sigs.get(p["signal_id"], {})
        entry, qty = f(p["entry"]), f(p["size"])
        sb = slip_bps(p["coin"])
        fill = entry * (1 - sb / 1e4)  # short sells slightly lower
        slip_cost = (entry - fill) * qty
        fee = fill * qty * CFG["TAKER_FEE"]
        ts = iso(int(f(s.get("candle_open_ms"))) + H1) if s else iso(now_ms())
        pos.append({"trade_id": p["plan_id"], "coin": p["coin"], "add_number": p["add_number"], "opened_at": ts,
                    "opened_ms": int(f(s.get("candle_open_ms"))) + H1, "entry": entry, "fill_price": fill,
                    "stop": p["stop"], "initial_stop": p["stop"], "tp": p["tp"], "qty": qty, "notional": p["notional"],
                    "risk_usd": p["risk_usd"], "risk_pct": p["risk_pct"],
                    "anchor_high": next((z["anchor_high"] for z in rows("zones") if z["zone_id"] == p["zone_id"]), ""),
                    "zone_id": p["zone_id"], "slip_bps": sb, "entry_costs": slip_cost + fee, "status": "OPEN",
                    "be_moved": "False", "last_checked_ms": int(f(s.get("candle_open_ms"))), "stale_flagged": "False"})
        journal([
            {"ts": ts, "trade_id": p["plan_id"], "coin": p["coin"], "event": "ENTRY", "side": "SHORT", "price": entry,
             "qty": qty, "notional": round(entry * qty, 4), "amount_usdt": 0, "note": "paper fill at 1H confirmation close"},
            {"ts": ts, "trade_id": p["plan_id"], "coin": p["coin"], "event": "SLIPPAGE", "side": "SHORT", "price": fill,
             "qty": qty, "notional": "", "amount_usdt": -round(slip_cost, 6), "note": f"{sb} bps adverse"},
            {"ts": ts, "trade_id": p["plan_id"], "coin": p["coin"], "event": "FEE", "side": "SHORT", "price": fill,
             "qty": qty, "notional": round(fill * qty, 4), "amount_usdt": -round(fee, 6), "note": "taker 0.05% entry"},
        ])
        opened.append(p["plan_id"])
    save("positions", pos)
    return opened


def close(p, price, reason, ts):
    """Mutates position p; journals EXIT/SLIPPAGE/FEE lines. Short covers -> buys slightly higher."""
    qty, entry = f(p["qty"]), f(p["entry"])
    sb = f(p["slip_bps"]) / 1e4
    fill = price * (1 + sb)
    gross = (entry - price) * qty
    slip = (fill - price) * qty
    fee = fill * qty * CFG["TAKER_FEE"]
    net = gross - slip - fee - f(p["entry_costs"])
    r = net / f(p["risk_usd"]) if f(p["risk_usd"]) else 0
    journal([
        {"ts": ts, "trade_id": p["trade_id"], "coin": p["coin"], "event": "EXIT", "side": "SHORT", "price": price,
         "qty": qty, "notional": round(price * qty, 4), "amount_usdt": round(gross, 6), "r_multiple": round(r, 4),
         "note": f"{reason}; net incl. all costs {net:+.4f} USDT"},
        {"ts": ts, "trade_id": p["trade_id"], "coin": p["coin"], "event": "SLIPPAGE", "side": "SHORT", "price": fill,
         "qty": qty, "notional": "", "amount_usdt": -round(slip, 6), "note": f"{p['slip_bps']} bps adverse"},
        {"ts": ts, "trade_id": p["trade_id"], "coin": p["coin"], "event": "FEE", "side": "SHORT", "price": fill,
         "qty": qty, "notional": round(fill * qty, 4), "amount_usdt": -round(fee, 6), "note": "taker 0.05% exit"},
    ])
    p.update({"status": "CLOSED", "closed_at": ts, "exit_price": price, "exit_reason": reason,
              "pnl_net": round(net, 6), "r_multiple": round(r, 4)})
    alert(f"## 📕 [PAPER] POSITION CLOSED — {p['coin']} — {ts}\n- Trade `{p['trade_id']}` closed by **{reason}** at "
          f"{fmt(price)} (entry {fmt(entry)})\n- Net P&L {net:+.2f} USDT = {r:+.2f}R (fees+slippage included)\n"
          f"- Paper only — no real order.")
    return {"trade_id": p["trade_id"], "coin": p["coin"], "reason": reason, "price": price,
            "pnl_net": round(net, 4), "r": round(r, 3)}


def be_trigger_price(p):
    """Price at which the stop moves to breakeven. 'price': BE_TRIGGER_R x stop distance below entry (old rule).
    'net': the price where covering would net +BE_TRIGGER_R x risk_usd after all fees and slippage (journal R)."""
    entry, R = f(p["entry"]), f(p["initial_stop"]) - f(p["entry"])
    if CFG["BE_TRIGGER_UNITS"] != "net":
        return entry - CFG["BE_TRIGGER_R"] * R
    qty, sb = f(p["qty"]), f(p["slip_bps"]) / 1e4
    return (entry * qty - f(p["entry_costs"]) - CFG["BE_TRIGGER_R"] * f(p["risk_usd"])) / (
        qty * (1 + sb) * (1 + CFG["TAKER_FEE"]))


def manage(client, until_ms):
    """Walk closed 1H candles (open_time > last_checked, close <= until_ms) for each open position."""
    pos = rows("positions")
    exits, events = [], []
    client.prefetch([(p["coin"], "1h", 300) for p in pos if p["status"] == "OPEN"])
    for p in pos:
        if p["status"] != "OPEN":
            continue
        try:
            d1 = client.klines(p["coin"], "1h", 300)
        except Exception as e:
            events.append(f"{p['coin']}: data error {e}")
            continue
        new = d1[(d1.open_time > int(f(p["last_checked_ms"]))) & (d1.open_time + H1 <= until_ms)]
        entry, R = f(p["entry"]), f(p["initial_stop"]) - f(p["entry"])
        trigger = be_trigger_price(p)
        mfe, mae = f(p.get("mfe_r")), f(p.get("mae_r"))  # excursions in price-R (stop distance units)
        for h in new.itertuples():
            stop, tp = f(p["stop"]), f(p["tp"])
            if h.high >= stop:  # conservative: stop checked before TP within the same candle
                # a stop the candle opened through fills at the open (e.g. BE armed on a wick, candle closed above it)
                px = max(stop, h.open) if CFG["GAP_AWARE_FILLS"] else stop
                mae = max(mae, (min(h.high, px) - entry) / R)
                p.update({"mfe_r": round(mfe, 3), "mae_r": round(mae, 3)})
                exits.append(close(p, px, "BE_STOP" if p["be_moved"] == "True" else "STOP", iso(h.open_time + H1)))
                break
            mae = max(mae, (h.high - entry) / R)
            if h.low <= tp:
                mfe = max(mfe, (entry - tp) / R)
                p.update({"mfe_r": round(mfe, 3), "mae_r": round(mae, 3)})
                exits.append(close(p, tp, "TP", iso(h.open_time + H1)))
                break
            mfe = max(mfe, (entry - h.low) / R)
            armed = {"wick": h.low <= trigger, "close": h.close <= trigger}.get(CFG["BE_MODE"], False)
            if p["be_moved"] != "True" and armed:
                p["stop"] = entry * (1 - 2 * CFG["TAKER_FEE"] - 2 * f(p["slip_bps"]) / 1e4)
                p["be_moved"] = "True"
                p["be_ms"] = int(h.open_time) + H1
                events.append(f"{p['coin']} {p['trade_id']}: +{CFG['BE_TRIGGER_R']}R ({CFG['BE_TRIGGER_UNITS']}, "
                              f"{CFG['BE_MODE']}) reached, stop -> breakeven {fmt(p['stop'])}")
            p["last_checked_ms"] = int(h.open_time)
        if p["status"] == "OPEN":
            p.update({"mfe_r": round(mfe, 3), "mae_r": round(mae, 3)})
    save("positions", pos)
    return exits, events


def unrealized(client, p):
    d1 = client.klines(p["coin"], "1h", 300)
    last = float(d1.iloc[-1].close)
    pnl = (f(p["entry"]) - last) * f(p["qty"]) - f(p["entry_costs"])
    return last, pnl, pnl / f(p["risk_usd"]) if f(p["risk_usd"]) else 0
