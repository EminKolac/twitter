"""ROLE 5 — Exit clerk. Writes EXIT NOW / STALE alerts. Paper positions are closed on EXIT NOW (1D close above anchor).
NEUTRAL while in profit -> HOLD. STALE (open >= 72h and flat) -> alert only, human decides; paper keeps holding."""
from . import paper
from .store import CFG, rows, save, iso, now_ms, f, alert, fmt, D1
from .rules import daily_trend_state


def run(client, daily: bool):
    pos = rows("positions")
    out = {"exit_now": [], "stale": [], "hold": [], "notes": []}
    for p in pos:
        if p["status"] != "OPEN":
            continue
        coin = p["coin"]
        try:
            if daily:
                d = client.klines(coin, "1d", 30)
                st = daily_trend_state(d, f(p["anchor_high"]))
                last = d.iloc[-1]
                # a 1D close above anchor missed by an earlier run (data error) still triggers EXIT NOW, at that close
                missed = d[(d.open_time + D1 > int(f(p["opened_ms"]))) & (d.close > f(p["anchor_high"]))]
                if len(missed):
                    st, last = "INVALID", missed.iloc[0]
                ts = iso(int(last.open_time) + D1)
                if st == "INVALID":
                    alert(f"## 🚨 EXIT NOW — {coin} — 1D close {fmt(last.close)} > anchor {fmt(p['anchor_high'])} ({ts})\n"
                          f"- Trade `{p['trade_id']}`: rule = close immediately. Paper position closed at the 1D close.\n"
                          f"- No real order placed.")
                    out["exit_now"].append(paper.close(p, float(last.close), "EXIT_NOW_1D_CLOSE_ABOVE_ANCHOR", ts))
                    continue
                _, pnl, r = paper.unrealized(client, p)
                if st == "NEUTRAL":
                    out["hold"].append(f"{coin}: 1D NEUTRAL, unrealized {r:+.2f}R -> " +
                                       ("HOLD (in profit)" if pnl > 0 else "hold; stop manages risk"))
            # stale check (every run)
            age_h = (now_ms() - int(f(p["opened_ms"]))) / 3_600_000
            if age_h >= CFG["STALE_HOURS"] and p["stale_flagged"] != "True":
                last, pnl, r = paper.unrealized(client, p)
                if abs(r) < CFG["STALE_R_BAND"]:
                    alert(f"## ⏳ STALE — {coin} — open {age_h:.0f}h, flat ({r:+.2f}R, last {fmt(last)})\n"
                          f"- Trade `{p['trade_id']}`: human decides (close or keep). Paper simulation keeps holding.")
                    p["stale_flagged"] = "True"
                    out["stale"].append({"trade_id": p["trade_id"], "coin": coin, "age_h": round(age_h), "r": round(r, 3)})
        except Exception as e:
            out["notes"].append(f"{coin}: {e}")
    save("positions", pos)
    return out
