"""ROLE 1 — Screener (runs on each 1D close, 00:00 UTC).
Scans top-N USDT perps by 24h quote volume for an active 1D three-candle bearish flip -> data/watchlist.csv.
Bullish flips -> data/context_bullish.csv (context only, never traded)."""
import time
from . import store
from .store import CFG, rows, save, append, iso, day, now_ms, log
from .rules import scan_daily, bounce_4h


def run(client):
    uni = client.universe(CFG["UNIVERSE_SIZE"])
    old = {r["coin"]: r for r in rows("watchlist")}
    ctx_seen = {(r["coin"], r["flip_date"]) for r in rows("context_bullish")}
    wl, ctx, failed = [], [], []
    ts = iso(now_ms())
    client.prefetch([(sym, "1d", 60) for sym, _ in uni])
    bears = []
    for sym, _ in uni:
        try:
            if scan_daily(client.klines(sym, "1d", 60), CFG["FLIP_LOOKBACK_DAYS"])["state"] == "BEAR":
                bears.append(sym)
        except Exception:
            pass
    # 4H (400 bars) for bearish coins: used for the bounce tag here and reused by the cartographer
    client.prefetch([(sym, "4h", 400) for sym in bears] + [(sym, "1h", 720) for sym in bears])
    for rank, (sym, qv) in enumerate(uni, 1):
        try:
            d = client.klines(sym, "1d", 60)
            if len(d) < 5:
                continue
            res = scan_daily(d, CFG["FLIP_LOOKBACK_DAYS"])
            if res["state"] == "BEAR":
                d4 = client.klines(sym, "4h", 400)
                bounce = bounce_4h(d4)
                wl.append({"coin": sym, "anchor_high": res["anchor"], "flip_date": day(res["flip_ms"]),
                           "tag": "PRIME" if bounce else "WATCH", "c1_open_ms": res["c1_open_ms"],
                           "impulse_low": res["impulse_low"], "last_close": float(d.iloc[-1].close),
                           "quote_volume_24h": round(qv), "volume_rank": rank, "bounce_4h": bounce, "updated_at": ts})
            elif res["state"] == "BULL":
                key = (sym, day(res["flip_ms"]))
                if key not in ctx_seen:
                    ctx.append({"coin": sym, "flip_date": key[1], "c1_low": res["c1_low"],
                                "last_close": float(d.iloc[-1].close), "logged_at": ts})
        except Exception as e:
            failed.append(sym)
            log.warning("screener %s failed: %s", sym, e)
    if len(failed) > len(uni) / 2:
        raise RuntimeError(f"screener: too many failures ({len(failed)}/{len(uni)}), watchlist not overwritten")
    # coins that failed this time keep their previous row (robust to transient API errors)
    for sym in failed:
        if sym in old:
            wl.append(old[sym])
    wl.sort(key=lambda r: (r["tag"] != "PRIME", int(r["volume_rank"])))
    save("watchlist", wl)
    append("context_bullish", ctx)
    new = sorted(set(r["coin"] for r in wl) - set(old))
    dropped = sorted(set(old) - set(r["coin"] for r in wl))
    return {"universe": len(uni), "watchlist": len(wl), "prime": sum(r["tag"] == "PRIME" for r in wl),
            "added": new, "dropped": dropped, "bullish_context_new": [c["coin"] for c in ctx], "failed": failed}


if __name__ == "__main__":
    import json
    from .data import DataClient
    store.ensure_files()
    print(json.dumps(run(DataClient()), indent=2, default=str))
