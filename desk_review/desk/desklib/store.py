"""Paths, locked config, CSV storage, journal + state helpers."""
import csv, json, os, logging
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
ALERTS = ROOT / "alerts"
CHARTS = ALERTS / "charts"
REPORTS = ROOT / "reports"
LOGS = ROOT / "logs"
for _p in (DATA, ALERTS, CHARTS, REPORTS, LOGS):
    _p.mkdir(parents=True, exist_ok=True)

ALERTS_FILE = ALERTS / "pending_alerts.md"
STATE_FILE = DATA / "state.json"

# ---- Desk configuration (mirrors DESK_RULES.md) ----
CFG = {
    "START_BALANCE": 1000.0,      # paper USDT
    "RISK_PCT": 1.0,              # % of paper balance per initial entry
    "ADD_RISK_PCT": 0.5,          # % per add (only when previous entry on coin is at breakeven)
    "MAX_ADDS": 2,                # max adds per coin on top of the initial entry
    "FLIP_ANCHOR_TOL": 0.005,     # closed legs count toward MAX_ADDS when their anchor is within 0.5% (same flip)
    "MAX_OPEN_COINS": 5,          # max coins with open paper positions
    "MAX_LEVERAGE": 5.0,          # per-position notional cap = 5x paper balance
    "MAX_GROSS_LEVERAGE": 10.0,   # all open notional cap = 10x paper balance
    "TAKER_FEE": 0.0005,          # 0.05% per side
    "SLIP_BPS_TOP20": 2.0,        # slippage per side, bps, top-20 by volume
    "SLIP_BPS_OTHER": 5.0,        # slippage per side, bps, everything else
    "STOP_BUFFER_PCT": 0.002,     # stop = zone_top + max(0.2% of zone_top, 0.25*ATR14(1H))
    "STOP_BUFFER_ATR": 0.25,
    "MIN_STOP_PCT": 0.0025,       # reject if stop distance < 0.25% of entry (fee-dominated)
    "MAX_STOP_PCT": 0.10,         # reject if stop distance > 10% of entry
    "MIN_BODY_FRAC": 0.30,        # BEARISH_INSIDE confirmation needs a real red body (>= 30% of 1H range)
    "MIN_RR": 1.0,                # reject if (entry - TP) / (stop - entry) < 1.0
    "BE_TRIGGER_R": 1.0,          # move stop to breakeven after +1R favourable excursion
    "UNIVERSE_SIZE": 100,         # top-N USDT perps by 24h quote volume
    "FLIP_LOOKBACK_DAYS": 20,     # look back this many 1D candles for an active flip
    "STALE_HOURS": 72,
    "STALE_R_BAND": 0.25,         # "flat" = |unrealized| < 0.25R
    "MAX_CATCHUP_HOURS": 48,      # 1H candles processed when catching up after downtime
    # ---- correctness / measurement (review 2026-10-26) ----
    "TRADE_LATEST_HOUR_ONLY": True,  # catch-up hours confirm (zone used) but are never traded: their zones/TP
                                     # were mapped with candles printed after the signal (lookahead)
    "GAP_AWARE_FILLS": True,      # a stop the 1H candle opens through fills at the open, not at the stop price
    "WIN_MIN_R": 0.10,            # auditor: a win nets >= +0.10R (strict subset of r > 0, can only lower win rate)
    # ---- correlated-entry risk control ----
    "MAX_NEW_PER_CANDLE": 2,      # max NEW coins approved per 1H confirmation candle
    "OPEN_RISK_BUDGET_PCT": 3.0,  # sum of risk_pct of open entries whose stop is not yet at breakeven
    # ---- hypotheses: OFF (= old behaviour) until the trade-log / replay tests support them ----
    "MAX_COST_SHARE": 0.0,        # >0: reject if round-trip costs > this share of 1R (test 0.15 / 0.20 / 0.30)
    "MIN_RR_NET": 0.0,            # >0: also require RR net of costs (same R unit as the journal)
    "BE_MODE": "wick",            # "wick" (old) | "close" (arm on a 1H close) | "none" (never; also disables adds)
    "BE_TRIGGER_UNITS": "price",  # "price" (old: 1x stop distance) | "net" (+BE_TRIGGER_R after all costs)
    "BTC_IMPULSE_ATR": 0.0,       # >0: no new coins when BTC 3h rise > k x ATR14(1H) (correlated squeeze)
    "ZONE_BREAK_ON_1H_CLOSE": False,  # True: a closed 1H candle above zone top stops that zone from confirming
    "MIN_PEN_FRAC": 0.0,          # >0: the wick must reach this fraction into the zone to count as a test
    "REJECT_MAX_CLOSE_LOC": 1.0,  # <1: REJECT needs a red candle or close in the lower part of its range
    "INSIDE_MAX_DEPTH": 1.0,      # <1: BEARISH_INSIDE must close in the lower part of the zone
    "MAX_FLIP_AGE_DAYS": 0,       # >0: no new coins (initial entries; adds exempt) when the flip's C3 is older
}

# ---- LOCKED auditor thresholds. Never changed by code. ----
THRESHOLDS = {"min_trades": 40, "min_win_rate": 0.33, "min_expectancy_r": 0.4, "max_dd_pct": 20.0}

H1 = 3_600_000
H4 = 4 * H1
D1 = 24 * H1
TF_MS = {"1h": H1, "4h": H4, "1d": D1}

SCHEMAS = {
    "watchlist": ["coin", "anchor_high", "flip_date", "tag", "c1_open_ms", "impulse_low", "last_close",
                  "quote_volume_24h", "volume_rank", "bounce_4h", "updated_at"],
    "context_bullish": ["coin", "flip_date", "c1_low", "last_close", "logged_at"],
    "zones": ["zone_id", "coin", "ladder", "zone_top", "zone_bottom", "formed_at", "formed_ms", "status",
              "tested_at", "invalidated_at", "confirmed", "anchor_high", "impulse_low", "updated_at"],
    "signals": ["signal_id", "coin", "zone_id", "zone_top", "zone_bottom", "candle_open_ms", "candle_time",
                "open", "high", "low", "close", "kind", "stop_level", "atr_1h", "anchor_high", "impulse_low",
                "processed"],
    "trade_plan": ["plan_id", "signal_id", "created_at", "coin", "zone_id", "entry", "stop", "tp", "size",
                   "notional", "risk_pct", "risk_usd", "implied_leverage", "rr", "add_number", "status", "reason",
                   "kind", "rr_net", "cost_share", "stop_atr", "cluster_n", "flip_age_d", "btc_x", "late_h"],
    "decisions": ["plan_id", "coin", "alert_time", "decision", "mode", "chart", "note"],
    "positions": ["trade_id", "coin", "add_number", "opened_at", "opened_ms", "entry", "fill_price", "stop",
                  "initial_stop", "tp", "qty", "notional", "risk_usd", "risk_pct", "anchor_high", "zone_id",
                  "slip_bps", "entry_costs", "status", "be_moved", "last_checked_ms", "stale_flagged",
                  "closed_at", "exit_price", "exit_reason", "pnl_net", "r_multiple",
                  "be_ms", "mfe_r", "mae_r"],
    "equity": ["ts", "balance", "unrealized", "equity", "open_positions"],
    "journal": ["ts", "trade_id", "coin", "event", "side", "price", "qty", "notional", "amount_usdt",
                "r_multiple", "balance_after", "note"],
}

logging.basicConfig(filename=str(LOGS / "desk.log"), level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("desk")


def now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def iso(ms) -> str:
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def day(ms) -> str:
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def f(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def rows(name):
    p = DATA / f"{name}.csv"
    if not p.exists() or p.stat().st_size == 0:
        return []
    with open(p, newline="") as fh:
        return list(csv.DictReader(fh))


def save(name, data):
    cols = SCHEMAS[name]
    p = DATA / f"{name}.csv"
    tmp = p.with_suffix(".csv.tmp")
    with open(tmp, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in data:
            w.writerow({c: ("" if r.get(c) is None else r.get(c)) for c in cols})
    os.replace(tmp, p)


def append(name, new_rows):
    if not new_rows:
        return
    save(name, rows(name) + list(new_rows))


def ensure_files():
    for name in SCHEMAS:
        if not (DATA / f"{name}.csv").exists():
            save(name, [])
    if not ALERTS_FILE.exists():
        ALERTS_FILE.write_text("# Pending alerts (PAPER DESK — alerts only, execution: NEVER)\n\n"
                               "Newest entries are appended at the bottom. No order is ever placed by this desk.\n\n")


def balance() -> float:
    return CFG["START_BALANCE"] + sum(f(r["amount_usdt"]) for r in rows("journal"))


def journal(entries):
    """entries: list of dicts (without balance_after). Appends with running balance."""
    bal = balance()
    out = []
    for e in entries:
        bal += f(e.get("amount_usdt"))
        e = dict(e)
        e["balance_after"] = round(bal, 4)
        out.append(e)
    append("journal", out)


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            log.exception("state file corrupt; starting fresh")
    return {}


def save_state(st):
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=2))
    os.replace(tmp, STATE_FILE)


def alert(text):
    with open(ALERTS_FILE, "a") as fh:
        fh.write(text.rstrip() + "\n\n---\n\n")


def fmt(x):
    x = float(x)
    if x == 0:
        return "0"
    a = abs(x)
    if a >= 1000:
        return f"{x:,.2f}"
    if a >= 1:
        return f"{x:.4f}"
    return f"{x:.6g}"
