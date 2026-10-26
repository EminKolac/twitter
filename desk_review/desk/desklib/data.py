"""Public market data client. Order: Binance USDT-M futures -> Bybit v5 linear -> OKX v5 SWAP.
No API keys, no authenticated endpoints, read-only GET requests only."""
import time, threading
from concurrent.futures import ThreadPoolExecutor
import requests
import pandas as pd
from .store import TF_MS, now_ms, log

STABLES = {"USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "USDE", "USD1", "RLUSD", "XUSD", "PYUSD"}
# tokenised gold / non-crypto proxies excluded even if an exchange lists them as "coin"
EXCLUDE_BASES = {"XAUT", "PAXG", "XAU", "XAG"}
MIN_GAP_S = 0.1  # polite throttle between public requests (all threads)
WORKERS = 6
_LOCK = threading.Lock()


class SourceBlocked(Exception):
    pass


class DataClient:
    ORDER = ["binance", "bybit", "okx"]

    def __init__(self):
        self.s = requests.Session()
        self.s.headers["User-Agent"] = "paper-desk/1.0 (public data only)"
        self.preferred = None
        self.cache = {}
        self.errors = []
        self.used = set()

    # ---------- http ----------
    def _get(self, url, params=None, tries=3):
        last = None
        for i in range(tries):
            try:
                with _LOCK:
                    gap = time.time() - getattr(self, "_last_req", 0)
                    if gap < MIN_GAP_S:
                        time.sleep(MIN_GAP_S - gap)
                    self._last_req = time.time()
                r = self.s.get(url, params=params, timeout=15)
                if r.status_code in (418, 429):  # rate-limit / temporary IP ban: back off, then fall back
                    time.sleep(min(float(r.headers.get("Retry-After", 3) or 3), 10))
                    raise RuntimeError(f"HTTP {r.status_code} (rate limited) {url}")
                if r.status_code in (403, 451):
                    raise SourceBlocked(f"HTTP {r.status_code} (geoblocked?) {url}")
                if r.status_code == 429 or r.status_code >= 500:
                    raise RuntimeError(f"HTTP {r.status_code} {url}")
                r.raise_for_status()
                return r.json()
            except SourceBlocked:
                raise
            except Exception as e:  # network / rate-limit -> backoff
                last = e
                time.sleep(1.5 * (i + 1))
        raise last

    def prefetch(self, jobs):
        """jobs: [(symbol, tf, limit)] fetched in parallel into the cache; failures are retried later on demand."""
        def one(j):
            try:
                self.klines(*j)
            except Exception:
                pass
        with ThreadPoolExecutor(WORKERS) as ex:
            list(ex.map(one, jobs))

    def _order(self):
        return ([self.preferred] if self.preferred else []) + [s for s in self.ORDER if s != self.preferred]

    # ---------- universe ----------
    def universe(self, n):
        """[(symbol, quote_volume_24h)] top-n USDT perpetuals by 24h quote volume."""
        for src in self._order():
            try:
                res = getattr(self, f"_{src}_universe")()
                res = [(s, v) for s, v in res if s[:-4] not in STABLES and s[:-4] not in EXCLUDE_BASES]
                res.sort(key=lambda x: -x[1])
                self.preferred = src
                self.used.add(src)
                return res[:n]
            except Exception as e:
                self.errors.append(f"universe/{src}: {e}")
                log.warning("universe via %s failed: %s", src, e)
        raise RuntimeError("all data sources failed for universe: " + "; ".join(self.errors[-3:]))

    def _binance_universe(self):
        info = self._get("https://fapi.binance.com/fapi/v1/exchangeInfo")
        ok = {s["symbol"] for s in info["symbols"]
              if s.get("contractType") == "PERPETUAL" and s.get("quoteAsset") == "USDT" and s.get("status") == "TRADING"
              and s.get("underlyingType", "COIN") == "COIN"}  # excludes TradFi (stocks/commodities/FX) perps
        tick = self._get("https://fapi.binance.com/fapi/v1/ticker/24hr")
        return [(t["symbol"], float(t["quoteVolume"])) for t in tick if t["symbol"] in ok]

    def _bybit_universe(self):
        info = self._get("https://api.bybit.com/v5/market/instruments-info", {"category": "linear", "limit": 1000})
        ok = {i["symbol"] for i in info["result"]["list"]
              if i.get("contractType") == "LinearPerpetual" and i.get("quoteCoin") == "USDT" and i.get("status") == "Trading"
              and i.get("symbolType", "") in ("", "innovation")}  # excludes stock/ETF/commodity/forex perps
        tick = self._get("https://api.bybit.com/v5/market/tickers", {"category": "linear"})
        return [(t["symbol"], float(t["turnover24h"])) for t in tick["result"]["list"] if t["symbol"] in ok]

    def _okx_universe(self):
        info = self._get("https://www.okx.com/api/v5/public/instruments", {"instType": "SWAP"})
        ok = {i["instId"] for i in info["data"] if i.get("state") == "live" and i.get("settleCcy") == "USDT"
              and i.get("instCategory", "1") in ("1", "")}  # instCategory 1 = crypto (others: stocks, metals, etc.)
        tick = self._get("https://www.okx.com/api/v5/market/tickers", {"instType": "SWAP"})
        out = []
        for t in tick["data"]:
            if t["instId"].endswith("-USDT-SWAP") and t["instId"] in ok:
                sym = t["instId"].replace("-USDT-SWAP", "USDT")
                out.append((sym, float(t["volCcy24h"]) * float(t["last"])))
        return out

    # ---------- klines ----------
    def klines(self, symbol, tf, limit):
        """Closed candles only, ascending. Columns: open_time(ms), open, high, low, close, volume."""
        key = (symbol, tf)
        if key in self.cache and self.cache[key].attrs.get("limit", 0) >= limit:
            return self.cache[key].tail(limit).reset_index(drop=True)
        last_err = None
        for src in self._order():
            try:
                raw = getattr(self, f"_{src}_klines")(symbol, tf, limit)
                df = pd.DataFrame(raw, columns=["open_time", "open", "high", "low", "close", "volume"])
                df = df.astype({"open_time": "int64", "open": float, "high": float, "low": float, "close": float, "volume": float})
                df = df.drop_duplicates("open_time").sort_values("open_time")
                df = df[df.open_time + TF_MS[tf] <= now_ms()].reset_index(drop=True)  # closed only
                if df.empty:
                    raise RuntimeError("no candles")
                df.attrs["limit"] = limit
                df.attrs["source"] = src
                self.cache[key] = df
                self.used.add(src)
                return df.tail(limit).reset_index(drop=True)
            except Exception as e:
                last_err = e
                self.errors.append(f"klines/{src}/{symbol}/{tf}: {e}")
        raise RuntimeError(f"klines failed for {symbol} {tf}: {last_err}")

    def _binance_klines(self, symbol, tf, limit):
        data = self._get("https://fapi.binance.com/fapi/v1/klines",
                         {"symbol": symbol, "interval": tf, "limit": min(limit + 1, 1500)})
        return [[k[0], k[1], k[2], k[3], k[4], k[5]] for k in data]

    def _bybit_klines(self, symbol, tf, limit):
        iv = {"1h": "60", "4h": "240", "1d": "D"}[tf]
        data = self._get("https://api.bybit.com/v5/market/kline",
                         {"category": "linear", "symbol": symbol, "interval": iv, "limit": min(limit + 1, 1000)})
        if data.get("retCode") != 0:
            raise RuntimeError(data.get("retMsg"))
        return [[k[0], k[1], k[2], k[3], k[4], k[5]] for k in data["result"]["list"]]

    def _okx_klines(self, symbol, tf, limit):
        bar = {"1h": "1H", "4h": "4H", "1d": "1Dutc"}[tf]
        inst = symbol[:-4] + "-USDT-SWAP"
        out = []
        data = self._get("https://www.okx.com/api/v5/market/candles", {"instId": inst, "bar": bar, "limit": 300})["data"]
        out += data
        while len(out) < limit + 1 and data:
            data = self._get("https://www.okx.com/api/v5/market/history-candles",
                             {"instId": inst, "bar": bar, "limit": 100, "after": out[-1][0]})["data"]
            out += data
        if not out:
            raise RuntimeError("okx empty")
        return [[k[0], k[1], k[2], k[3], k[4], k[5]] for k in out]
