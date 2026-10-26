#!/workspace/desk/.venv/bin/python
"""Single entrypoint for the PAPER desk. Safe to run any time (idempotent): it works out which UTC closes
(1H / 4H / 1D) happened since the last run (data/state.json) and runs the matching pipeline:
  1D close : screener -> cartographer(zones) -> paper stop/TP walk up to the daily close -> exit clerk (daily)
  4H close : cartographer(zones) -> exit clerk
  1H close : per closed hour, in event order: paper exits through that candle -> cartographer(confirm) ->
             risk officer (only the newest hour is tradable) -> gate (alerts+charts) -> paper fills; then exit clerk
  --weekly : auditor weekly report
NO EXECUTION. NO API KEYS. Public market data only."""
import argparse, fcntl, json, sys, traceback
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from desklib import store, screener, cartographer, risk_officer, gate, paper, exit_clerk, auditor
from desklib.store import H1, H4, D1, now_ms, iso, rows, f, CFG
from desklib.data import DataClient


def last_closed(tf_ms, now):
    return (now // tf_ms) * tf_ms - tf_ms  # open time of the most recent fully closed candle (UTC aligned)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--weekly", action="store_true", help="auditor weekly report vs locked thresholds")
    ap.add_argument("--force", nargs="*", choices=["1h", "4h", "1d"], default=[], help="re-run steps even if no new close")
    a = ap.parse_args()
    store.ensure_files()
    lock = open(store.DATA / ".lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(json.dumps({"status": "busy", "msg": "another run_desk.py is running"}))
        return 0
    if a.weekly:
        print(json.dumps(auditor.weekly(), indent=2, default=str))
        return 0

    st = store.load_state()
    now = now_ms()
    latest = {"1d": last_closed(D1, now), "4h": last_closed(H4, now), "1h": last_closed(H1, now)}
    due = {tf: (st.get(tf) is None or latest[tf] > st[tf] or tf in a.force) for tf in latest}
    client = DataClient()
    S = {"run_at": iso(now), "mode": "PAPER (execution: NEVER)", "closes_processed": {}, "steps": [], "errors": [],
         "new_alerts": [], "rejected": [], "exits": [], "stale": [], "hold": [], "events": []}

    def step(name, fn):
        try:
            r = fn()
            S["steps"].append(name)
            return r, True
        except Exception as e:
            S["errors"].append(f"{name}: {e}")
            store.log.error("%s failed: %s\n%s", name, e, traceback.format_exc())
            return None, False

    carto_done = False
    if due["1d"]:
        r, ok1 = step("screener", lambda: screener.run(client))
        if ok1:
            S["screener"] = r
        r, ok2 = step("cartographer.zones", lambda: cartographer.run_zones(client))
        if ok2:
            S["zones"] = r
            carto_done = True
        r, _ = step("paper.manage(to 1D close)", lambda: paper.manage(client, latest["1d"] + D1))
        if r:
            S["exits"] += r[0]; S["events"] += r[1]
        r, ok3 = step("exit_clerk.daily", lambda: exit_clerk.run(client, daily=True))
        if r:
            S["exits"] += r["exit_now"]; S["stale"] += r["stale"]; S["hold"] += r["hold"]; S["events"] += r["notes"]
        if ok1 and ok2 and ok3:
            st["1d"] = latest["1d"]; S["closes_processed"]["1d"] = iso(latest["1d"] + D1)
    if due["4h"]:
        ok = True
        if not carto_done:
            r, ok = step("cartographer.zones", lambda: cartographer.run_zones(client))
            if ok:
                S["zones"] = r
        r, ok2 = step("exit_clerk.4h", lambda: exit_clerk.run(client, daily=False))
        if r:
            S["stale"] += r["stale"]; S["events"] += r["notes"]
        if ok and ok2:
            st["4h"] = latest["4h"]; S["closes_processed"]["4h"] = iso(latest["4h"] + H4)
    if due["1h"]:
        start = latest["1h"] if st.get("1h") is None or "1h" in a.force else st["1h"] + H1
        start = max(start, latest["1h"] - (CFG["MAX_CATCHUP_HOURS"] - 1) * H1)
        hours = list(range(start, latest["1h"] + 1, H1))
        oks = []
        S["signals"] = []
        for h in hours:  # walk the book THROUGH candle h first, then size entries filled at h's close
            r, ok = step("paper.manage", lambda h=h: paper.manage(client, h + H1)); oks.append(ok)
            if r:
                S["exits"] += r[0]; S["events"] += r[1]
            r, ok = step("cartographer.confirm", lambda h=h: cartographer.confirm(client, [h])); oks.append(ok)
            if r:
                S["signals"] += r["new_signals"]
            r, ok = step("risk_officer", lambda: risk_officer.run(client, latest["1h"])); oks.append(ok)
            if r:
                S["rejected"] += r["rejected"]
            r, ok = step("gate", lambda: gate.run(client)); oks.append(ok)
            if r:
                S["new_alerts"] += r["new_alerts"]
            r, ok = step("paper.open", paper.open_new); oks.append(ok)
        r, ok = step("exit_clerk.1h", lambda: exit_clerk.run(client, daily=False)); oks.append(ok)
        if r:
            S["stale"] += r["stale"]
        if all(oks):
            st["1h"] = latest["1h"]; S["closes_processed"]["1h"] = [iso(h + H1) for h in hours]
    st["last_run"] = iso(now)
    st["data_sources_used"] = sorted(client.used)
    store.save_state(st)

    opens = []
    for p in rows("positions"):
        if p["status"] == "OPEN":
            try:
                last, pnl, rr = paper.unrealized(client, p)
            except Exception:
                last, pnl, rr = None, None, None
            opens.append({"trade_id": p["trade_id"], "coin": p["coin"], "add": p["add_number"], "entry": f(p["entry"]),
                          "stop": f(p["stop"]), "tp": f(p["tp"]), "qty": f(p["qty"]), "be_moved": p["be_moved"] == "True",
                          "last": last, "unrealized_usdt": round(pnl, 2) if pnl is not None else None,
                          "unrealized_r": round(rr, 2) if rr is not None else None})
    # mark-to-market equity point (realized balance + open P&L at the last 1H close) for the auditor's MTM drawdown;
    # unrealized() deducts entry costs that the balance already holds, which roughly stands in for exit costs
    if all(o["unrealized_usdt"] is not None for o in opens):
        bal = store.balance()
        unr = sum(o["unrealized_usdt"] for o in opens)
        store.append("equity", [{"ts": iso(now), "balance": round(bal, 4), "unrealized": round(unr, 4),
                                 "equity": round(bal + unr, 4), "open_positions": len(opens)}])
    stx = auditor.stats()
    S.update({
        "data_sources_used": sorted(client.used),
        "watchlist": [{"coin": w["coin"], "tag": w["tag"], "anchor_high": f(w["anchor_high"]), "flip_date": w["flip_date"],
                       "last_close": f(w["last_close"])} for w in rows("watchlist")],
        "live_zones": sum(z["status"] != "INVALIDATED" for z in rows("zones")),
        "open_positions": opens,
        "balance_usdt": round(stx["balance"], 2),
        "closed_trades": stx["trades"],
        "alerts_file": str(store.ALERTS_FILE),
    })
    out = json.dumps(S, indent=2, default=str)
    (store.DATA / "last_summary.json").write_text(out)
    with open(store.LOGS / "runs.jsonl", "a") as fh:
        fh.write(json.dumps(S, default=str) + "\n")
    print(out)
    if client.errors:
        store.log.warning("data errors this run: %s", client.errors[:20])
    return 1 if S["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
