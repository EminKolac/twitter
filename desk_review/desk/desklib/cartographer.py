"""ROLE 2 — Cartographer.
run_zones(): on 1D/4H closes, maps 4H bearish FVGs across the impulse (anchor high -> current low) for every
watchlist coin and every coin with an open paper position -> data/zones.csv (ladder 1 = top).
confirm(): on 1H closes, checks each newly closed 1H candle against live zones -> data/signals.csv."""
from .store import CFG, H1, rows, save, append, iso, now_ms, f, log
from .rules import bearish_fvgs, zone_status, confirm_1h, atr, stop_level, penetration


def _targets():
    t = {}
    for r in rows("watchlist"):
        t[r["coin"]] = {"anchor": f(r["anchor_high"]), "start": int(f(r["c1_open_ms"]))}
    for p in rows("positions"):
        if p["status"] == "OPEN" and p["coin"] not in t:
            z = next((z for z in rows("zones") if z["zone_id"] == p["zone_id"]), None)
            start = int(f(z["formed_ms"])) - 10 * 86_400_000 if z else now_ms() - 20 * 86_400_000
            t[p["coin"]] = {"anchor": f(p["anchor_high"]), "start": start}
    return t


def run_zones(client):
    prev = {z["zone_id"]: z for z in rows("zones")}
    # a zone confirms once, even if it was dropped (watchlist churn) and is now re-created
    used = {s["zone_id"]: s["candle_time"] for s in rows("signals")}
    out, failed = [], []
    ts = iso(now_ms())
    tg = _targets()
    client.prefetch([(c, "4h", 400) for c in tg] + [(c, "1h", 720) for c in tg])
    for coin, t in tg.items():
        try:
            d4 = client.klines(coin, "4h", 400)
            d1 = client.klines(coin, "1h", 720)
            imp = d1[d1.open_time >= t["start"]]
            impulse_low = float(imp.low.min()) if len(imp) else float(d4.low.min())
            zs = bearish_fvgs(d4, t["start"], t["anchor"])
            zs.sort(key=lambda z: -z["zone_top"])
            for lad, z in enumerate(zs, 1):
                zid = f"{coin}-{z['mid_ms']}"
                status, tested_ms, inv_ms = zone_status(z, d4, d1)
                p = prev.get(zid, {})
                out.append({"zone_id": zid, "coin": coin, "ladder": lad, "zone_top": z["zone_top"],
                            "zone_bottom": z["zone_bottom"], "formed_at": iso(z["formed_ms"]), "formed_ms": z["formed_ms"],
                            "status": status, "tested_at": iso(tested_ms) if tested_ms else p.get("tested_at", ""),
                            "invalidated_at": iso(inv_ms) if inv_ms else "", "confirmed": p.get("confirmed") or used.get(zid, ""),
                            "anchor_high": t["anchor"], "impulse_low": impulse_low, "updated_at": ts})
        except Exception as e:
            failed.append(coin)
            log.warning("cartographer %s failed: %s", coin, e)
            out += [z for z in prev.values() if z["coin"] == coin]  # keep old zones on API error
    save("zones", out)
    live = [z for z in out if z["status"] != "INVALIDATED"]
    return {"coins": len(_targets()), "zones_total": len(out), "zones_live": len(live),
            "fresh": sum(z["status"] == "FRESH" for z in out), "tested": sum(z["status"] == "TESTED" for z in out),
            "failed": failed}


def confirm(client, hours):
    """hours: list of 1H candle open times (ms) that closed since the last run, ascending."""
    zones = rows("zones")
    # zones kept only to manage an open position (coin off the watchlist) never generate signals
    wl = {w["coin"] for w in rows("watchlist")}
    used = {s["zone_id"] for s in rows("signals")}  # zones that already signalled never confirm again
    by_coin = {}
    for z in zones:
        if z["status"] != "INVALIDATED" and not z["confirmed"] and z["coin"] in wl and z["zone_id"] not in used:
            by_coin.setdefault(z["coin"], []).append(z)
    seen = {r["signal_id"] for r in rows("signals")}
    client.prefetch([(c, "1h", 300) for c in by_coin])
    sigs, failed = [], []
    for coin, zs in by_coin.items():
        zs.sort(key=lambda z: -f(z["zone_top"]))
        try:
            d1 = client.klines(coin, "1h", 300)
        except Exception as e:
            failed.append(coin)
            log.warning("confirm %s failed: %s", coin, e)
            continue
        for h_ms in hours:
            idx = d1.index[d1.open_time == h_ms]
            if not len(idx):
                continue
            i = int(idx[0])
            h = d1.iloc[i]
            a = atr(d1.iloc[max(0, i - 15):i + 1])
            # zones that existed (formed) before this candle, not yet confirmed/invalidated
            live = [z for z in zs if int(f(z["formed_ms"])) <= h_ms and not z["confirmed"]]
            # mark tests
            for z in live:
                if h.high >= f(z["zone_bottom"]) and not z["tested_at"]:
                    z["tested_at"] = iso(h_ms)
                    if z["status"] == "FRESH":
                        z["status"] = "TESTED"
            if CFG["ZONE_BREAK_ON_1H_CLOSE"]:
                # a closed 1H candle above zone top (before this one) -> zone no longer confirms
                hist = client.klines(coin, "1h", 720)  # zones can be ~22 days old; 300 bars cover 12.5
                prior = hist[hist.open_time < h_ms]
                live = [z for z in live
                        if not (prior[prior.open_time >= int(f(z["formed_ms"]))].close > f(z["zone_top"])).any()]
            touched = [z for z in live if h.high >= f(z["zone_bottom"])
                       and penetration(h.high, f(z["zone_top"]), f(z["zone_bottom"])) >= CFG["MIN_PEN_FRAC"]]
            if not touched:
                continue
            z = touched[0]  # highest zone the wick reached (deep enough, if MIN_PEN_FRAC is set)
            stp = stop_level(f(z["zone_top"]), a)
            kind = confirm_1h(h, f(z["zone_top"]), f(z["zone_bottom"]), stp)
            if not kind:
                continue
            sid = f"{z['zone_id']}-{h_ms}"
            if sid in seen:
                continue
            z["confirmed"] = iso(h_ms)
            since = d1[(d1.open_time >= int(f(z["formed_ms"]))) & (d1.open_time <= h_ms)]
            imp_low = min([x for x in (f(z["impulse_low"]), float(since.low.min()) if len(since) else 0) if x > 0])
            sigs.append({"signal_id": sid, "coin": coin, "zone_id": z["zone_id"], "zone_top": z["zone_top"],
                         "zone_bottom": z["zone_bottom"], "candle_open_ms": h_ms, "candle_time": iso(h_ms),
                         "open": h.open, "high": h.high, "low": h.low, "close": h.close, "kind": kind,
                         "stop_level": stp, "atr_1h": a, "anchor_high": z["anchor_high"], "impulse_low": imp_low,
                         "processed": ""})
    save("zones", zones)
    append("signals", sigs)
    return {"hours_checked": [iso(h) for h in hours], "new_signals": [s["signal_id"] for s in sigs], "failed": failed}


if __name__ == "__main__":
    import json
    from .data import DataClient
    from . import store
    store.ensure_files()
    print(json.dumps(run_zones(DataClient()), indent=2, default=str))
