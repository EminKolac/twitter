"""Pure rule logic (no I/O): three-candle 1D flip, 4H bearish FVGs, zone status, 1H confirmation."""
import pandas as pd
from .store import H4, CFG


def is_bear_flip(c1, c2, c3) -> bool:
    """C1 closes down. C2 closes below C1 close, C2 high < C1 high. C3 closes below C2 close, C3 high < C1 high.
    'Do not take out / do not touch' -> strict '<'. C2/C3 may be green or doji as long as closes step down."""
    return (c1["close"] < c1["open"]
            and c2["close"] < c1["close"] and c2["high"] < c1["high"]
            and c3["close"] < c2["close"] and c3["high"] < c1["high"])


def is_bull_flip(c1, c2, c3) -> bool:
    """Mirror image (context only, never traded)."""
    return (c1["close"] > c1["open"]
            and c2["close"] > c1["close"] and c2["low"] > c1["low"]
            and c3["close"] > c2["close"] and c3["low"] > c1["low"])


def scan_daily(d: pd.DataFrame, lookback: int):
    """d: closed 1D candles ascending. Walk back from the newest candle (C3 = newest possible).
    Returns most recent ACTIVE bearish flip (not invalidated by a 1D close above anchor), unless a more recent
    bullish flip exists (then state BULL = context only)."""
    n = len(d)
    lo = max(2, n - lookback)
    for i in range(n - 1, lo - 1, -1):
        c1, c2, c3 = d.iloc[i - 2], d.iloc[i - 1], d.iloc[i]
        if is_bull_flip(c1, c2, c3):
            return {"state": "BULL", "flip_ms": int(c3.open_time), "c1_low": float(c1.low)}
        if is_bear_flip(c1, c2, c3):
            anchor = float(c1.high)
            after = d.iloc[i + 1:]
            if (after.close > anchor).any():
                continue  # invalidated, look further back
            impulse_low = float(d.iloc[i - 2:].low.min())
            return {"state": "BEAR", "anchor": anchor, "c1_open_ms": int(c1.open_time),
                    "flip_ms": int(c3.open_time), "impulse_low": impulse_low}
    return {"state": "NONE"}


def daily_trend_state(d: pd.DataFrame, anchor: float) -> str:
    """For exits on the latest closed 1D candle: INVALID (close > anchor), BEARISH (close < prior close), else NEUTRAL."""
    last, prev = d.iloc[-1], d.iloc[-2]
    if last.close > anchor:
        return "INVALID"
    if last.close < prev.close:
        return "BEARISH"
    return "NEUTRAL"


def bounce_4h(d4: pd.DataFrame) -> bool:
    """4H 'bouncing' = last closed 4H candle is green AND closed above the close 3 candles earlier (12h)."""
    if len(d4) < 4:
        return False
    last = d4.iloc[-1]
    return bool(last.close > last.open and last.close > d4.iloc[-4].close)


def bearish_fvgs(d4: pd.DataFrame, start_ms: int, anchor: float):
    """3-candle bearish FVG on 4H: candle A low > candle C high -> zone [C.high, A.low].
    Scanned across the full impulse (from C1's 1D open to now). Zone top is clipped to anchor."""
    d = d4[d4.open_time >= start_ms].reset_index(drop=True)
    out = []
    for k in range(2, len(d)):
        a, b, c = d.iloc[k - 2], d.iloc[k - 1], d.iloc[k]
        if a.low > c.high:
            top, bottom = min(float(a.low), anchor), float(c.high)
            if bottom >= top:
                continue
            out.append({"zone_top": top, "zone_bottom": bottom, "mid_ms": int(b.open_time),
                        "formed_ms": int(c.open_time) + H4})
    return out


def zone_status(z, d4: pd.DataFrame, d1: pd.DataFrame):
    """INVALIDATED: a closed 4H candle after formation closes above zone_top.
    TESTED: any closed 1H or 4H candle after formation (before invalidation) has high >= zone_bottom (partial wick counts).
    FRESH: otherwise."""
    fm = z["formed_ms"]
    later4 = d4[d4.open_time >= fm]
    inv = later4[later4.close > z["zone_top"]]
    inv_ms = int(inv.open_time.min()) if len(inv) else None
    cutoff = inv_ms + H4 if inv_ms is not None else None
    h = d1[d1.open_time >= fm]
    if cutoff:
        h = h[h.open_time < cutoff]
        later4 = later4[later4.open_time <= inv_ms]
    t = pd.concat([h[h.high >= z["zone_bottom"]].open_time, later4[later4.high >= z["zone_bottom"]].open_time])
    tested_ms = int(t.min()) if len(t) else None
    status = "INVALIDATED" if inv_ms is not None else ("TESTED" if tested_ms is not None else "FRESH")
    return status, tested_ms, inv_ms


def atr(d: pd.DataFrame, n=14) -> float:
    pc = d.close.shift(1)
    tr = pd.concat([d.high - d.low, (d.high - pc).abs(), (d.low - pc).abs()], axis=1).max(axis=1)
    return float(tr.tail(n).mean())


def stop_level(zone_top: float, atr_1h: float) -> float:
    return zone_top + max(CFG["STOP_BUFFER_PCT"] * zone_top, CFG["STOP_BUFFER_ATR"] * atr_1h)


def confirm_1h(h, zone_top, zone_bottom, stop):
    """1H confirmation on a closed candle that wicks into the zone (this wick is itself a valid test):
      REJECT        : high >= zone_bottom and close < zone_bottom  (wick in, close back below zone)
      BEARISH_INSIDE: high >= zone_bottom, red candle with body >= 30% of its range, zone_bottom <= close <= zone_top
    and in both cases high < stop level (the candle must not run the stop). Returns kind or None."""
    if h["high"] < zone_bottom or h["high"] >= stop:
        return None
    rng = h["high"] - h["low"]
    if h["close"] < zone_bottom:
        # optional: a REJECT must look like one (red, or close in the lower part of the candle's range)
        if CFG["REJECT_MAX_CLOSE_LOC"] < 1.0 and not (
                h["close"] < h["open"] or (rng > 0 and (h["close"] - h["low"]) / rng <= CFG["REJECT_MAX_CLOSE_LOC"])):
            return None
        return "REJECT"
    body = h["open"] - h["close"]
    depth = (h["close"] - zone_bottom) / (zone_top - zone_bottom) if zone_top > zone_bottom else 0.0
    if (body > 0 and rng > 0 and body / rng >= CFG["MIN_BODY_FRAC"] and zone_bottom <= h["close"] <= zone_top
            and depth <= CFG["INSIDE_MAX_DEPTH"]):
        return "BEARISH_INSIDE"
    return None


def penetration(high, zone_top, zone_bottom) -> float:
    """How far the wick reached into the zone, as a fraction of zone height (0 = touched the bottom, 1 = top)."""
    if zone_top <= zone_bottom:
        return 1.0
    return (min(high, zone_top) - zone_bottom) / (zone_top - zone_bottom)
