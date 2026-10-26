
# DESK RULES — single source of truth (PAPER DESK)

Source: @0xNevsky, "Grok Bot for Traders: The 6-Bot Desk" (6-role system), implemented as a **paper-only**,
rule-based desk. Code constants live in `desklib/store.py` (`CFG`, `THRESHOLDS`) and must match this file.

## 0. Hard limits
- **Execution: NEVER.** No exchange account, no API keys, no order placement, ever. Alerts only.
- Data: free public market data only — Binance USDT-M futures public REST (`fapi.binance.com`), fallback Bybit v5
  public linear, then OKX v5 public SWAP. All timestamps UTC.
- Paper account: starts at **1000 USDT**. Fills are simulated.

## 1. Market
- USDT perpetual futures. **Short-only.**
- Universe: TRADING USDT-margined perpetuals, top **100 by 24h quote volume** (stablecoin bases excluded), rescanned
  at every 1D close.

## 2. Trend filter (1D) — three-candle rule
On closed 1D candles C1, C2, C3 (consecutive):
1. C1 closes down (close < open).
2. C2 closes below C1's close; C2 does **not** take out C1's high (C2 high < C1 high).
3. C3 body closes below C2's close (C3 close < C2 close); C2 and C3 do **not touch** C1's high (strict `<`).
- Dojis valid (C2/C3 may be dojis as long as the closes step down). C2 need not be red. C1 must be red.
- **Anchor high = C1 high** (chosen over "swing high of impulse": it is exactly the level rules 2–3 protect, so it is
  unambiguous and is the invalidation level).
- A flip stays **active** while no 1D candle has closed above the anchor. Screener looks back 20 daily candles for the
  most recent active flip; if a more recent **bullish** mirror flip exists, the coin is not on the watchlist (bullish
  flips are logged to `context_bullish.csv` as context only, never traded).
- Tag **PRIME** = active 1D down-flip while 4H is bouncing (last closed 4H candle green AND its close > the close 3
  candles earlier). Otherwise tag **WATCH**.

## 3. Zones (4H)
- Bearish FVG (3-candle): candles A, B, C consecutive with **A.low > C.high** → zone `[C.high, A.low]`.
- Scanned across the **full impulse**: from C1's 1D open to now, between anchor high and current low (zone top clipped
  to anchor). Laddered **top to bottom** (ladder 1 = highest).
- Status: **FRESH** (untouched) → **TESTED** when any later closed 1H/4H candle's high ≥ zone bottom (**a partial wick
  counts as a test**) → **INVALIDATED** when a closed 4H candle closes above zone top.

## 4. Confirmation (1H)
On a newly closed 1H candle whose high reaches into a live (non-invalidated, unconfirmed) zone — the wick itself is
the test — using the **highest** zone the wick reached:
- **REJECT**: high ≥ zone bottom and close < zone bottom (wick in, close back below), or
- **BEARISH_INSIDE**: red candle (close < open) whose body is ≥ 30% of its high–low range, closing inside the zone
  (zone bottom ≤ close ≤ zone top) — filters doji-like noise while price drifts inside a wide zone,
- and in both cases high < stop level (the candle must not run the stop).
Each zone can confirm only once — also after the zone was dropped and re-created (zone ids already in
`signals.csv` never confirm again). Only coins on the **current watchlist** generate signals; zones kept solely to
manage an open position never do.
Optional, OFF by default (test first): `ZONE_BREAK_ON_1H_CLOSE` (a closed 1H candle above zone top stops the zone
from confirming), `MIN_PEN_FRAC` (minimum wick penetration into the zone), `REJECT_MAX_CLOSE_LOC` (REJECT must be red
or close in the lower part of its range), `INSIDE_MAX_DEPTH` (BEARISH_INSIDE must close in the lower part of the zone).

## 5. Risk
- Initial entry risk **1%** of current paper balance. **Add 0.5%** only if the previous entry on that coin is at
  breakeven (stop already moved to BE). Max 2 adds per coin **per flip (same anchor), open or closed** — closed legs
  no longer free up add slots; add numbers are unique per flip. Max 5 coins open.
- **Correlated entries:** max **2 new coins per 1H confirmation candle**; the risk of open entries whose stop is not yet
  at breakeven may not exceed **3%** of balance (in-batch approvals count).
- **Only the newest closed 1H candle is tradable.** Hours replayed after downtime are still confirmed (the zone is used)
  but their plans are rejected as late: zones / TP / watchlist were rebuilt with candles printed after them.
- **Stop = zone top + max(0.2% × zone top, 0.25 × ATR14(1H))**.
- Position size = risk USDT / (stop − entry + est. round-trip fees + slippage) → the 1% includes costs.
- Reject if: stop distance < 0.25% of entry (fee-dominated) or > 10%; RR to TP < 1.0; add without BE; max coins/adds.
- **Leverage cap:** per-position notional ≤ **5×** paper balance, all open notional ≤ **10×**. If capped, size is cut
  (actual risk % is lower and reported). Implied leverage = notional / balance is reported on every plan/alert.
- **Breakeven:** after price moves +1R in favour, stop → entry minus round-trip fees+slippage (true breakeven).
  `BE_MODE` = `wick` (current: a 1H low reaching the trigger) | `close` (a 1H close beyond it) | `none` (never — this
  also disables adds, which require BE). `BE_TRIGGER_UNITS` = `price` (current: 1 × stop distance) | `net` (+1R after
  all costs, the same R the journal reports). Change only after the BE replay test (see review README).
- Optional, OFF by default (test first): `MAX_COST_SHARE` (reject when round-trip costs exceed this share of 1R),
  `MIN_RR_NET` (RR net of costs), `MAX_FLIP_AGE_DAYS`, `BTC_IMPULSE_ATR` (no new coins after a BTC 3h rise > k × ATR).
- Every plan logs its decision-time features: kind, rr_net, cost_share, stop/ATR, same-candle cluster size, flip age,
  BTC 3h impulse, signal lateness.

## 6. Exits
- **1D close above anchor → EXIT NOW** (paper closes at that daily close).
- 1D **NEUTRAL** (no close above anchor, last close ≥ previous close) **while in profit → HOLD**.
- **Flat 72h → STALE** flag (open ≥ 72h and |unrealized| < 0.25R) → alert, **human decides**; paper keeps holding.
- **Take-profit = impulse low** (lowest low from C1 open to the confirmation candle), single full exit.
- Stop / TP are walked on closed 1H candles; if both are hit in the same candle, **stop is assumed first**.
- A stop the candle **opens through** fills at that candle's open, not at the stop price (e.g. BE armed on a wick of a
  candle that closed back above the BE level). Positions record MFE / MAE (in stop-distance units) and the BE time.
- Every daily run re-checks all 1D closes since the position opened, so an EXIT NOW missed by a data error is applied
  at the first close above the anchor.

## 7. Execution / paper fills
- Never executes. Each approved plan → alert in `alerts/pending_alerts.md` + chart PNG; paper mode auto-records TAKEN.
- Paper fill at the confirmation 1H **close**. Fees **0.05% taker each side**. Slippage per side: **2 bps** (top-20 by
  volume) / **5 bps** (others), always adverse. ENTRY/EXIT, FEE and SLIPPAGE are separate journal lines.

## 8. Auditor — LOCKED thresholds (never changed by code)
| Metric | Threshold |
|---|---|
| Closed paper trades | ≥ 40 |
| Win rate (a win nets **≥ +0.10R**; breakeven scratches are not wins) | ≥ 33% |
| Expectancy | ≥ +0.4R (net of fees & slippage) |
| Max drawdown (worse of realized balance and mark-to-market equity) | ≤ 20% |
Below 40 trades the verdict is "INSUFFICIENT SAMPLE". Going live is never automatic.
The win-rate and drawdown definitions above are **stricter** than the original ones (r > 0 counted breakeven exits that
round to +0.0001R as wins; drawdown ignored open P&L). Thresholds are unchanged. The old figures stay in the weekly
report as context only.


