#!/usr/bin/env python3
"""
Multi-source Solana new-launch sniper (PAPER + LIVE PHANTOM MODE + REAL-TIME WS STREAMING).

Sources:
- Real-Time WebSockets: PumpPortal WS (sub-second launches + trade ticks) & Solana RPC logsSubscribe
- HTTP Polling: GeckoTerminal new pools (ALL Solana DEXes), Jupiter recent, DexScreener profiles/boosts, PumpPortal
Rug screen: on-chain mint/freeze authority (Solana RPC) + Jupiter audit + RugCheck.
Live Mode: Phantom Browser-Once automation via `PhantomBrowserBridge` (Jupiter Swap + auto-confirm).
"""
import csv
import io
import json
import math
import os
import re
import shutil
import sys
import threading
import time
from datetime import datetime

from http_client import HttpSession
from phantom_bridge import LAMPORTS_PER_SOL, PhantomBrowserBridge
from realtime_stream import RealtimeStreamManager

if sys.stdout.encoding != "utf-8":
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
    except Exception:
        pass

CONFIG = {
    # mode & wallet
    "trading_mode": "paper",  # "paper" or "live"
    "live_trade_size_sol": 0.05,
    "live_max_sol_per_trade": 0.25,
    "live_dry_run": False,
    "phantom_profile_dir": "phantom_profile",
    "phantom_extension_path": "",
    "phantom_auto_confirm": True,
    # account & risk
    "start_balance_usd": 50.0, "trade_size_usd": 5.0, "max_open_positions": 3,
    "daily_loss_limit_usd": 10.0,
    "take_profit_pct": 50.0, "trailing_stop_pct": 0.0, "max_hold_minutes": 120,
    # sniper window: buy ONLY coins younger than this
    "max_age_minutes": 2.0,
    # coins younger than this skip the liquidity / buy-volume floors (first-seconds entries)
    "ultra_early_grace_sec": 10,
    # scan speed
    "scan_interval_sec": 1, "monitor_interval_sec": 1.0,
    # entry guards (applied after the grace window)
    "min_liquidity_usd": 300.0, "min_volume_m5_usd": 0.0,
    "min_buys_m5": 2, "min_buy_sell_ratio_m5": 1.0,
    "max_price_change_m5_pct": 300.0,
    "min_holders": 0,
    # hard rug screen
    "use_rugcheck": True, "strict_rugcheck": True, "rugcheck_max_score": 50.0,
    "max_top10_holder_pct": 75.0, "rug_recheck_minutes": 15,
    # candidate sources & streaming
    "use_realtime_stream": True, "use_solana_ws": True,
    "use_gecko": True, "use_jupiter_recent": True, "use_dex_profiles": True,
    "use_pumpportal": True, "use_promoted_feeds": False,
    # misc
    "slippage_pct": 2.0, "fee_pct": 1.0,
    "state_file": "state.json", "trades_csv": "trades.csv",
    "live_state_file": "live_state.json", "live_trades_csv": "live_trades.csv",
    "log_rejects": True, "jupiter_api_key": "",
    # compatibility keys kept so old settings files still load
    "cooldown_minutes": 60, "min_volume_h1_usd": 0.0, "min_age_minutes": 0,
    "max_age_hours": 24, "min_buys_h1": 0, "min_buy_sell_ratio_h1": 0.0,
    "min_price_change_h1_pct": -100.0, "max_price_change_h1_pct": 10000.0,
    "use_jupiter_price": True, "pool_keep_hours": 1, "pool_max_size": 500,
}

DEX = "https://api.dexscreener.com"
GECKO = "https://api.geckoterminal.com/api/v2/networks/solana/new_pools"
JUP = "https://api.jup.ag/tokens/v2"
JUP_PRICE = "https://api.jup.ag/price/v3"
RUGC = "https://api.rugcheck.xyz/v1/tokens"
PUMPP = "https://api.pumpportal.fun/api/data"
RPC_URLS = [
    "https://api.mainnet-beta.solana.com",
    "https://solana-rpc.publicnode.com",
]
HEADERS = {"User-Agent": "solana-launch-sniper/3.0", "Accept": "application/json"}
_session = HttpSession()

SEEN_TTL_SEC = 10


def now():
    return datetime.now().astimezone()


def fmt_price(x):
    if x is None:
        return "n/a"
    x = float(x)
    if x == 0:
        return "0.00"
    if abs(x) >= 1:
        return f"{x:,.4f}"
    decimals = min(10, max(2, -math.floor(math.log10(abs(x))) + 5))
    return f"{x:.{decimals}f}"


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


CONSOLE_OUTPUT = True
LOG_HOOK = None
_live_active = False
_print_lock = threading.Lock()


def log(msg):
    global _live_active
    with _print_lock:
        if _live_active:
            width = shutil.get_terminal_size((120, 20)).columns - 1
            print("\r" + " " * width + "\r", end="")
            _live_active = False
        line = f"[{now().strftime('%H:%M:%S')}] {msg}"
        if CONSOLE_OUTPUT:
            try:
                print(line, flush=True)
            except UnicodeEncodeError:
                print(line.encode("ascii", "replace").decode("ascii"), flush=True)
        if LOG_HOOK:
            try:
                LOG_HOOK(line)
            except Exception:
                pass


def get_json(url, params=None, timeout=6, headers=None):
    try:
        r = _session.get(url, params=params, headers={**HEADERS, **(headers or {})}, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


# ---------------------------------------------------------------- candidate sources
_src_last, _src_stats = {}, {}
_src_lock = threading.Lock()
_seen, _seen_lock = {}, threading.Lock()
_src_of = {}


def _mark_seen(mints):
    t = time.time()
    with _seen_lock:
        for m in mints:
            _seen[m] = t
        if len(_seen) > 20000:
            for k in [k for k, v in _seen.items() if t - v > 600]:
                _seen.pop(k, None)


def _fresh_only(mints):
    t = time.time()
    with _seen_lock:
        return [m for m in mints if t - _seen.get(m, 0) > SEEN_TTL_SEC]


def _src_gecko(cfg):
    if not cfg.get("use_gecko", True):
        return []
    out = []
    for page in (1, 2):
        data = get_json(GECKO, params={"page": page})
        if not (isinstance(data, dict) and isinstance(data.get("data"), list)):
            continue
        for pool in data["data"]:
            try:
                rel = pool.get("relationships") or {}
                tid = ((rel.get("base_token") or {}).get("data") or {}).get("id", "")
                if tid.startswith("solana_"):
                    mint = tid[len("solana_"):]
                    if mint and len(mint) > 20:
                        out.append(mint)
            except Exception:
                continue
    return out


def _src_jup(cfg):
    if not cfg.get("use_jupiter_recent", True):
        return []
    hdrs = {"x-api-key": cfg["jupiter_api_key"]} if cfg.get("jupiter_api_key") else None
    data = get_json(f"{JUP}/recent", headers=hdrs)
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = data.get("tokens") or data.get("data") or []
    else:
        return []
    return [t.get("id") for t in items if isinstance(t, dict) and t.get("id")]


def _src_profiles(cfg):
    if not cfg.get("use_dex_profiles", True):
        return []
    data = get_json(f"{DEX}/token-profiles/latest/v1")
    if not isinstance(data, list):
        return []
    return [
        d.get("tokenAddress")
        for d in data
        if isinstance(d, dict) and d.get("chainId") == "solana" and d.get("tokenAddress")
    ]


def _src_boosts(cfg):
    if not cfg.get("use_promoted_feeds", False):
        return []
    data = get_json(f"{DEX}/token-boosts/latest/v1")
    if not isinstance(data, list):
        return []
    return [
        d.get("tokenAddress")
        for d in data
        if isinstance(d, dict) and d.get("chainId") == "solana" and d.get("tokenAddress")
    ]


def _src_pumpportal(cfg):
    if not cfg.get("use_pumpportal", True):
        return []
    data = get_json(f"{PUMPP}/coins/new")
    if not isinstance(data, list):
        return []
    return [c.get("mint") for c in data if isinstance(c, dict) and c.get("mint")]


SOURCES = [
    ("gecko", 5.0, _src_gecko),
    ("jup", 5.0, _src_jup),
    ("profiles", 3.0, _src_profiles),
    ("pumpportal", 3.0, _src_pumpportal),
    ("boosts", 10.0, _src_boosts),
]


def fetch_candidate_mints(cfg, force=False, stream_mgr=None):
    mints, lock = set(), threading.Lock()

    # Drain sub-second real-time WebSocket detections first
    if stream_mgr is not None:
        streamed = stream_mgr.pop_new_mints(max_items=60)
        for m, src in streamed:
            if isinstance(m, str) and 32 <= len(m) <= 50:
                mints.add(m)
                _src_of[m] = src

    def run(name, cd, fn):
        t = time.time()
        with _src_lock:
            if not force and t - _src_last.get(name, 0) < cd:
                return
            _src_last[name] = t
        try:
            res = fn(cfg) or []
            _src_stats[name] = {"items": len(res), "ok_at": time.time()}
        except Exception:
            _src_stats[name] = {"items": -1, "ok_at": 0.0}
            res = []
        with lock:
            for m in res:
                if isinstance(m, str) and 32 <= len(m) <= 50:
                    mints.add(m)
                    if m not in _src_of:
                        _src_of[m] = name
                    if len(_src_of) > 20000:
                        _src_of.clear()

    ths = [threading.Thread(target=run, args=s, daemon=True) for s in SOURCES]
    for t in ths:
        t.start()
    for t in ths:
        t.join(timeout=5)
    fresh = _fresh_only(list(mints))
    _mark_seen(mints)
    return fresh


# ---------------------------------------------------------------- entry filters
def passes_filters(pair, cfg, now_ms=None):
    """Returns (ok, short_reason_code, detail)."""
    now_ms = now_ms or int(time.time() * 1000)
    created = int(pair.get("pairCreatedAt") or 0)
    if created <= 0:
        return False, "missing data", "no pairCreatedAt"
    age_s = (now_ms - created) / 1000.0
    if age_s > cfg.get("max_age_minutes", 2) * 60:
        return False, "too old", f"{age_s / 60:.1f}m > window"
    if age_s < -10:
        return False, "clock skew", "sync your PC clock"
    if _f(pair.get("priceUsd")) is None:
        return False, "no price", "no price on DexScreener yet"

    m5 = (pair.get("txns") or {}).get("m5") or {}
    buys, sells = int(m5.get("buys") or 0), int(m5.get("sells") or 0)
    ratio = cfg.get("min_buy_sell_ratio_m5", 1.0)
    if ratio > 0 and buys < sells * ratio:
        return False, "selling pressure", f"{buys}b/{sells}s in last 5m"
    m5c = _f((pair.get("priceChange") or {}).get("m5"))
    if m5c is not None and m5c > cfg.get("max_price_change_m5_pct", 300):
        return False, "already pumped", f"+{m5c:.0f}% in 5m"

    if age_s > cfg.get("ultra_early_grace_sec", 10):
        liq = _f((pair.get("liquidity") or {}).get("usd")) or 0.0
        if liq < cfg.get("min_liquidity_usd", 300):
            return False, "low liquidity", f"${liq:.0f} < ${cfg.get('min_liquidity_usd', 0):.0f}"
        if buys < cfg.get("min_buys_m5", 2):
            return False, "few buys", f"{buys} buys in last 5m"
        if cfg.get("min_volume_m5_usd", 0) > 0:
            mv5 = _f((pair.get("volume") or {}).get("m5")) or 0.0
            if mv5 < cfg["min_volume_m5_usd"]:
                return False, "low m5 volume", f"${mv5:.0f}"
        if cfg.get("min_holders", 0) > 0:
            h1b = int(((pair.get("txns") or {}).get("h1") or {}).get("buys") or 0)
            if h1b < cfg["min_holders"]:
                return False, "too few h1 buys", f"{h1b}"
    return True, "ok", ""


# ---------------------------------------------------------------- hard rug screen
_rug_cache, _rug_lock = {}, threading.Lock()


def _rpc_mint_authorities(mint):
    """Direct on-chain check of the SPL mint account. Returns (rpc_ok, mint_auth, freeze_auth)."""
    body = {"jsonrpc": "2.0", "id": 1, "method": "getAccountInfo", "params": [mint, {"encoding": "jsonParsed"}]}
    for url in RPC_URLS:
        try:
            r = _session.post(url, json=body, timeout=6, headers={**HEADERS, "Content-Type": "application/json"})
            if r.status_code != 200:
                continue
            value = ((r.json().get("result") or {}).get("value") or {})
            parsed = ((value.get("data") or {}).get("parsed") or {})
            if parsed.get("type") == "mint":
                inf = parsed.get("info") or {}
                return True, inf.get("mintAuthority"), inf.get("freezeAuthority")
        except Exception:
            continue
    return False, None, None


def hard_rug_check(mint, cfg, age_s):
    """Return (ok, reason). On-chain authority check + Jupiter audit + RugCheck. Cached."""
    ttl = max(60, cfg.get("rug_recheck_minutes", 15) * 60)
    with _rug_lock:
        hit = _rug_cache.get(mint)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]

    verdict = (True, "ok")
    rpc_ok, ma, fa = _rpc_mint_authorities(mint)
    if rpc_ok:
        if ma:
            verdict = (False, "mint authority NOT revoked (on-chain: dev can mint supply)")
        elif fa:
            verdict = (False, "freeze authority enabled (on-chain: dev can freeze sells)")
    else:
        hdrs = {"x-api-key": cfg["jupiter_api_key"]} if cfg.get("jupiter_api_key") else None
        jt = get_json(f"{JUP}/search", params={"query": mint}, headers=hdrs)
        if isinstance(jt, dict):
            jt = jt.get("tokens") or jt.get("data") or []
        if isinstance(jt, list) and jt:
            tok = next((t for t in jt if isinstance(t, dict) and t.get("id") == mint), None)
            if tok is None and isinstance(jt[0], dict):
                tok = jt[0]
            if isinstance(tok, dict):
                if tok.get("mintAuthority"):
                    verdict = (False, "mint authority NOT revoked")
                elif tok.get("freezeAuthority"):
                    verdict = (False, "freeze authority enabled")

    young = age_s < 180  # seconds-old pools always look 'low liquidity' to RugCheck
    if verdict[0] and cfg.get("use_rugcheck", True):
        rep = get_json(f"{RUGC}/{mint}/report/summary", timeout=8)
        risks = rep.get("risks") if isinstance(rep, dict) else None
        if isinstance(risks, list):
            if not young:
                sn = _f(rep.get("score_normalised"))
                if sn is not None:
                    if sn > 100:
                        sn /= 1000.0
                    if sn >= cfg.get("rugcheck_max_score", 50):
                        verdict = (False, f"rugcheck risk score {sn:.0f}/100")
            if verdict[0]:
                strict = cfg.get("strict_rugcheck", True)
                for risk in risks:
                    level = str(risk.get("level") or "").lower()
                    name = str(risk.get("name") or "")
                    low = name.lower()
                    if level != "danger":
                        if "holder" in low and not young:
                            m = re.search(r"([\d.]+)\s*%", str(risk.get("description") or "") + " " + name)
                            if m and float(m.group(1)) > cfg.get("max_top10_holder_pct", 75):
                                verdict = (False, f"{name} ({m.group(1)}%)")
                                break
                        continue
                    if young and (("liquidity" in low) or ("lp" in low)):
                        continue
                    if not strict and not (("mint" in low) or ("freeze" in low) or ("authorit" in low) or ("holder" in low)):
                        continue
                    verdict = (False, f"rugcheck danger: {name or 'unknown risk'}")
                    break
        elif not young:
            verdict = (False, "no rugcheck report (coin not brand new)")

    with _rug_lock:
        if len(_rug_cache) > 5000:
            _rug_cache.clear()
        _rug_cache[mint] = (time.time(), verdict)
    return verdict


# ---------------------------------------------------------------- accounts (Paper & Live)
def locked(fn):
    def wrapper(self, *args, **kwargs):
        with self.lock:
            return fn(self, *args, **kwargs)

    return wrapper


class PaperAccount:
    mode = "paper"

    def __init__(self, cfg, stream_mgr=None):
        self.cfg = cfg
        self.stream_mgr = stream_mgr
        self.lock = threading.RLock()
        self.path = cfg["state_file"]
        self.state = {
            "cash": cfg["start_balance_usd"],
            "positions": {},
            "day": now().strftime("%Y-%m-%d"),
            "day_pnl": 0.0,
            "total_pnl": 0.0,
            "closed": 0,
            "wins": 0,
            "blacklist": [],
        }
        self.load()

    def load(self):
        if os.path.exists(self.path):
            with open(self.path, encoding="utf-8") as f:
                self.state.update(json.load(f))
            self.state.setdefault("blacklist", [])

    @locked
    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.state, f, indent=2, ensure_ascii=False)
        os.replace(tmp, self.path)

    def _roll_day(self):
        if self.state["day"] != now().strftime("%Y-%m-%d"):
            self.state["day"] = now().strftime("%Y-%m-%d")
            self.state["day_pnl"] = 0.0

    def can_open(self):
        self._roll_day()
        c = self.cfg
        if len(self.state["positions"]) >= c["max_open_positions"]:
            return False, "max positions"
        if self.state["cash"] < c["trade_size_usd"]:
            return False, "no cash"
        if self.state["day_pnl"] <= -c["daily_loss_limit_usd"]:
            return False, "daily loss limit"
        return True, "ok"

    def _csv_path(self):
        return self.cfg["trades_csv"]

    def _log_trade(self, row):
        path = self._csv_path()
        new = not os.path.exists(path)
        with open(path, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new:
                w.writerow([
                    "time", "action", "symbol", "mint", "price", "usd",
                    "pnl_usd", "pnl_pct", "reason", "dexscreener", "age_s", "dex", "source",
                ])
            w.writerow(row)

    @locked
    def note_rejected(self, mint):
        bl = self.state["blacklist"]
        if mint not in bl:
            bl.append(mint)
        if len(bl) > 5000:
            del bl[: len(bl) - 5000]
        self.save()

    @locked
    def buy(self, mint, symbol, price, url=None, meta=None):
        meta = meta or {}
        c = self.cfg
        url = url or f"https://dexscreener.com/solana/{mint}"
        ok, _why = self.can_open()
        if not ok or mint in self.state["positions"]:
            return False
        size = c["trade_size_usd"]
        fill = price * (1 + c["slippage_pct"] / 100)
        qty = (size * (1 - c["fee_pct"] / 100)) / fill
        self.state["cash"] -= size
        self.state["positions"][mint] = {
            "symbol": symbol,
            "qty": qty,
            "entry_price": fill,
            "cost": size,
            "opened": time.time(),
            "url": url,
            "peak_price": fill,
            "low_price": fill,
            "dex": meta.get("dex", "?"),
            "source": meta.get("source", "?"),
            "age_at_entry": round(meta.get("age_s", 0), 1),
        }
        self.state["blacklist"].append(mint)
        self._log_trade([
            now().isoformat(), "BUY", symbol, mint, fmt_price(fill), size, "", "",
            "sniper_entry", url, meta.get("age_s", 0), meta.get("dex", "?"), meta.get("source", "?"),
        ])
        log(f"✓ SNIPER BUY {symbol} [{meta.get('dex', '?')}/{meta.get('source', '?')}] age {meta.get('age_s', 0):.0f}s @ ${fmt_price(fill)}")
        if self.stream_mgr:
            self.stream_mgr.subscribe_token(mint)
        self.save()
        return True

    @locked
    def sell(self, mint, price, reason):
        c = self.cfg
        pos = self.state["positions"].pop(mint)
        fill = price * (1 - c["slippage_pct"] / 100)
        proceeds = pos["qty"] * fill * (1 - c["fee_pct"] / 100)
        pnl = proceeds - pos["cost"]
        pnl_pct = pnl / pos["cost"] * 100
        self.state["cash"] += proceeds
        self._roll_day()
        self.state["day_pnl"] += pnl
        self.state["total_pnl"] += pnl
        self.state["closed"] += 1
        if pnl > 0:
            self.state["wins"] += 1
        self._log_trade([
            now().isoformat(), "SELL", pos["symbol"], mint, fmt_price(fill),
            proceeds, round(pnl, 4), round(pnl_pct, 2), reason, pos.get("url", ""),
        ])
        log(f"✗ SELL {pos['symbol']} @ ${fmt_price(fill)} pnl ${pnl:.2f} ({pnl_pct:.1f}%) [{reason}]")
        if self.stream_mgr:
            self.stream_mgr.unsubscribe_token(mint)
        self.save()

    @locked
    def check_exits(self, prices):
        exits = 0
        tp = self.cfg.get("take_profit_pct", 50)
        trail = self.cfg.get("trailing_stop_pct", 0)
        for mint, pos in list(self.state["positions"].items()):
            price = prices.get(mint)
            if price is None:
                continue
            entry = pos["entry_price"]
            pos["peak_price"] = max(pos.get("peak_price", entry), price)
            pos["low_price"] = min(pos.get("low_price", entry), price)
            change = (price / entry - 1) * 100
            peak_chg = (pos["peak_price"] / entry - 1) * 100
            reason = None
            if tp > 0 and change >= tp:
                reason = f"take_profit_{tp:g}"
            elif trail > 0 and peak_chg >= 10 and (pos["peak_price"] / price - 1) * 100 >= trail:
                reason = f"trailing_stop_{trail:g}"
            elif (time.time() - pos["opened"]) / 60 >= self.cfg.get("max_hold_minutes", 120):
                reason = "max_hold"
            if reason:
                self.sell(mint, price, reason)
                exits += 1
        return exits


class LiveAccount(PaperAccount):
    """
    Live Solana trading account backed by Phantom Browser-Once automation.
    Executes Jupiter swaps through the persistent Phantom browser session,
    checks SOL balance and safety caps, and persists positions in `live_state.json`.
    """

    mode = "live"

    def __init__(self, cfg, bridge, stream_mgr=None):
        self.bridge = bridge
        self.cfg = cfg
        self.stream_mgr = stream_mgr
        self.lock = threading.RLock()
        self.path = cfg.get("live_state_file", "live_state.json")
        self.state = {
            "cash": 0.0,
            "positions": {},
            "day": now().strftime("%Y-%m-%d"),
            "day_pnl": 0.0,
            "total_pnl": 0.0,
            "closed": 0,
            "wins": 0,
            "blacklist": [],
        }
        self.load()
        self.sync_wallet_cash()

    def _csv_path(self):
        return self.cfg.get("live_trades_csv", "live_trades.csv")

    def _sol_price(self):
        if self.stream_mgr and self.stream_mgr.sol_price_usd > 1.0:
            return self.stream_mgr.sol_price_usd
        return 180.0

    def sync_wallet_cash(self):
        if self.bridge and self.bridge.wallet_address:
            sol = self.bridge.sol_balance
            self.state["cash"] = round(sol * self._sol_price(), 2)
        return self.state["cash"]

    def can_open(self):
        self._roll_day()
        c = self.cfg
        if not self.bridge or not self.bridge.connected_once or not self.bridge.wallet_address:
            return False, "phantom not connected"
        if len(self.state["positions"]) >= c["max_open_positions"]:
            return False, "max positions"
        trade_sol = float(c.get("live_trade_size_sol", 0.05))
        max_sol = float(c.get("live_max_sol_per_trade", 0.25))
        if trade_sol <= 0 or trade_sol > max_sol:
            return False, "trade SOL exceeds cap"
        if not c.get("live_dry_run", False) and self.bridge.sol_balance < (trade_sol + 0.005):
            return False, "insufficient SOL"
        if self.state["day_pnl"] <= -c["daily_loss_limit_usd"]:
            return False, "daily loss limit"
        return True, "ok"

    @locked
    def buy(self, mint, symbol, price, url=None, meta=None):
        meta = meta or {}
        c = self.cfg
        url = url or f"https://dexscreener.com/solana/{mint}"
        ok, why = self.can_open()
        if not ok or mint in self.state["positions"]:
            return False

        trade_sol = float(c.get("live_trade_size_sol", 0.05))
        lamports = int(trade_sol * LAMPORTS_PER_SOL)
        slippage_bps = int(float(c.get("slippage_pct", 2.0)) * 100)
        dry_run = bool(c.get("live_dry_run", False))

        log(f"⚡ LIVE BUY {symbol} via Phantom ({trade_sol:.4f} SOL, dry_run={dry_run})...")
        res = self.bridge.execute_swap("BUY", mint, lamports, slippage_bps=slippage_bps, dry_run=dry_run)
        if not res.get("ok"):
            log(f"✗ LIVE BUY FAILED {symbol}: {res.get('error')}")
            return False

        sol_usd = self._sol_price()
        cost_usd = trade_sol * sol_usd
        qty_est = cost_usd / max(price, 1e-12)
        out_raw = int(res.get("out_amount") or 0)
        sig = res.get("signature", "")

        self.sync_wallet_cash()
        self.state["positions"][mint] = {
            "symbol": symbol,
            "qty": qty_est,
            "raw_amount": out_raw,
            "entry_price": price,
            "cost": cost_usd,
            "cost_sol": trade_sol,
            "opened": time.time(),
            "url": url,
            "peak_price": price,
            "low_price": price,
            "dex": meta.get("dex", "?"),
            "source": meta.get("source", "?"),
            "age_at_entry": round(meta.get("age_s", 0), 1),
            "tx_sig": sig,
        }
        self.state["blacklist"].append(mint)
        self._log_trade([
            now().isoformat(), "LIVE_BUY", symbol, mint, fmt_price(price), round(cost_usd, 2), "", "",
            f"phantom:{sig[:12]}", url, meta.get("age_s", 0), meta.get("dex", "?"), meta.get("source", "?"),
        ])
        log(f"✓ LIVE SNIPER BUY {symbol} sig={sig[:16]}... @ ${fmt_price(price)}")
        if self.stream_mgr:
            self.stream_mgr.subscribe_token(mint)
        self.save()
        return True

    @locked
    def sell(self, mint, price, reason):
        c = self.cfg
        pos = self.state["positions"].get(mint)
        if not pos:
            return
        raw_amount = int(pos.get("raw_amount") or 0)
        if raw_amount <= 0:
            # Estimate raw units (6 decimals default for pump/spl) if dry run quote omitted it
            raw_amount = max(1, int(pos["qty"] * 1_000_000))
        slippage_bps = int(float(c.get("slippage_pct", 2.0)) * 100)
        dry_run = bool(c.get("live_dry_run", False))

        res = self.bridge.execute_swap("SELL", mint, raw_amount, slippage_bps=slippage_bps, dry_run=dry_run)
        if not res.get("ok"):
            log(f"✗ LIVE SELL FAILED {pos['symbol']}: {res.get('error')}")
            return

        self.state["positions"].pop(mint, None)
        sol_usd = self._sol_price()
        out_sol = int(res.get("out_amount") or 0) / LAMPORTS_PER_SOL
        proceeds = (out_sol * sol_usd) if out_sol > 0 else (pos["qty"] * price)
        pnl = proceeds - pos["cost"]
        pnl_pct = pnl / max(pos["cost"], 1e-9) * 100
        self.sync_wallet_cash()
        self._roll_day()
        self.state["day_pnl"] += pnl
        self.state["total_pnl"] += pnl
        self.state["closed"] += 1
        if pnl > 0:
            self.state["wins"] += 1
        sig = res.get("signature", "")
        self._log_trade([
            now().isoformat(), "LIVE_SELL", pos["symbol"], mint, fmt_price(price),
            round(proceeds, 2), round(pnl, 4), round(pnl_pct, 2), f"{reason}:{sig[:10]}", pos.get("url", ""),
        ])
        log(f"✗ LIVE SELL {pos['symbol']} @ ${fmt_price(price)} pnl ${pnl:.2f} ({pnl_pct:.1f}%) [{reason}]")
        if self.stream_mgr:
            self.stream_mgr.unsubscribe_token(mint)
        self.save()


# ---------------------------------------------------------------- scan / monitor
_stats_lock = threading.Lock()
_stats = {"since": time.time(), "cands": 0, "no_pair": 0, "checked": 0, "rejects": {}, "rug": 0, "bought": 0}
_last_block_log = [0.0]


def _maybe_summary():
    with _stats_lock:
        if time.time() - _stats["since"] < 60:
            return
        cands, no_pair, checked = _stats["cands"], _stats["no_pair"], _stats["checked"]
        rejects, rug, bought = dict(_stats["rejects"]), _stats["rug"], _stats["bought"]
        src_bits = [
            f"{n}={v.get('items', 0)}{'*' if time.time() - v.get('ok_at', 0) > 120 else ''}"
            for n, v in sorted(_src_stats.items())
        ]
        _stats.update({"since": time.time(), "cands": 0, "no_pair": 0, "checked": 0, "rejects": {}, "rug": 0, "bought": 0})
    rej_txt = " ".join(f"{k}:{v}" for k, v in sorted(rejects.items(), key=lambda kv: -kv[1]))
    log(
        f"scan summary (60s): sources[{(' '.join(src_bits)) or 'none'}] "
        f"new-mints={cands} not-on-dexscreener={no_pair} evaluated={checked} "
        f"rejected[{rej_txt or '-'}] rug={rug} BOUGHT={bought}"
    )


def scan_and_buy(acct, cfg, stream_mgr=None):
    _maybe_summary()
    ok, why = acct.can_open()
    if not ok:
        if time.time() - _last_block_log[0] > 30:
            _last_block_log[0] = time.time()
            log(f"Cannot trade: {why}" + (" — sell/close positions in Open positions" if why == "max positions" else ""))
        return
    fresh = fetch_candidate_mints(cfg, stream_mgr=stream_mgr)
    if not fresh:
        return
    with _stats_lock:
        _stats["cands"] += len(fresh)

    now_ms = int(time.time() * 1000)
    pairs = {}
    for i in range(0, len(fresh), 30):
        chunk = fresh[i : i + 30]
        data = get_json(f"{DEX}/tokens/v1/solana/{','.join(chunk)}")
        if not isinstance(data, list):
            continue
        for p in data:
            mint = (p.get("baseToken") or {}).get("address")
            if not mint:
                continue
            liq = _f((p.get("liquidity") or {}).get("usd")) or 0.0
            cur = pairs.get(mint)
            if cur is None or liq > cur[0]:
                pairs[mint] = (liq, p)
    with _stats_lock:
        _stats["no_pair"] += max(0, len(fresh) - len(pairs))

    for mint, (_liq, pair) in pairs.items():
        if not acct.can_open()[0]:
            break
        if mint in acct.state["positions"] or mint in acct.state["blacklist"]:
            continue
        symbol = (pair.get("baseToken") or {}).get("symbol") or "?"
        okp, code, detail = passes_filters(pair, cfg, now_ms)
        with _stats_lock:
            _stats["checked"] += 1
        if not okp:
            with _stats_lock:
                _stats["rejects"][code] = _stats["rejects"].get(code, 0) + 1
            if cfg.get("log_rejects"):
                log(f"– {symbol} filtered: {code} ({detail})")
            continue
        age_s = max(0.0, (now_ms - int(pair.get("pairCreatedAt") or 0)) / 1000.0)
        rok, rwhy = hard_rug_check(mint, cfg, age_s)
        if not rok:
            with _stats_lock:
                _stats["rug"] += 1
            log(f"✗ RUG REJECT {symbol}: {rwhy}")
            acct.note_rejected(mint)
            continue
        price = _f(pair.get("priceUsd"))
        if price is None:
            continue
        meta = {"dex": pair.get("dexId") or "?", "source": _src_of.get(mint, "?"), "age_s": age_s}
        if acct.buy(mint, symbol, price, pair.get("url"), meta):
            with _stats_lock:
                _stats["bought"] += 1


def _jupiter_prices(mints, cfg):
    if not cfg.get("use_jupiter_price", True) or not mints:
        return {}
    headers = {"x-api-key": cfg["jupiter_api_key"]} if cfg.get("jupiter_api_key") else None
    data = get_json(JUP_PRICE, params={"ids": ",".join(mints)}, timeout=4, headers=headers)
    if not isinstance(data, dict):
        return {}
    prices = {}
    for mint, quote in data.items():
        if isinstance(quote, dict):
            price = _f(quote.get("usdPrice"))
            if price is not None:
                prices[mint] = price
    return prices


def monitor(acct, stream_mgr=None):
    pos = acct.state["positions"]
    if not pos:
        return {}
    mints = list(pos.keys())
    prices = {}

    # 1. Sub-second real-time WebSocket stream prices
    if stream_mgr is not None:
        ws_prices = stream_mgr.get_live_prices(max_age_sec=8.0)
        for m in mints:
            if m in ws_prices:
                prices[m] = ws_prices[m]

    # 2. Jupiter live prices for remaining mints
    missing_jup = [m for m in mints if m not in prices]
    if missing_jup:
        prices.update(_jupiter_prices(missing_jup, acct.cfg))

    # 3. DexScreener fallback
    missing = [mint for mint in mints if mint not in prices]
    for i in range(0, len(missing), 30):
        chunk = missing[i : i + 30]
        data = get_json(f"{DEX}/tokens/v1/solana/{','.join(chunk)}", timeout=4)
        if isinstance(data, list):
            for p in data:
                mint = (p.get("baseToken") or {}).get("address")
                if mint in pos and mint not in prices:
                    price = _f(p.get("priceUsd"))
                    if price is not None:
                        prices[mint] = price
    acct.check_exits(prices)
    return prices


def probe(cfg, stream_mgr=None):
    """One-shot diagnostic: what candidates exist right now and exactly why each is/isn't bought."""
    log("--- PROBE: force-fetching all sources now ---")
    cands = fetch_candidate_mints(cfg, force=True, stream_mgr=stream_mgr)
    log(f"probe: {len(cands)} fresh mints right now")
    if not cands:
        log("probe: 0 candidates → all sources returned nothing (internet/VPN/CF block?)")
        return {"candidates": 0, "items": []}
    now_ms = int(time.time() * 1000)
    grace = cfg.get("ultra_early_grace_sec", 10)
    results = []
    for i in range(0, len(cands), 30):
        chunk = cands[i : i + 30]
        data = get_json(f"{DEX}/tokens/v1/solana/{','.join(chunk)}")
        plist = data if isinstance(data, list) else []
        if not plist:
            log(f"probe: no pair data on DexScreener for {len(chunk)} mints (too new / not indexed yet)")
        for p in plist:
            sym = ((p.get("baseToken") or {}).get("symbol") or "?")[:10]
            created = int(p.get("pairCreatedAt") or 0)
            age = (now_ms - created) / 1000.0 if created else -1
            liq = _f((p.get("liquidity") or {}).get("usd")) or 0.0
            m5 = (p.get("txns") or {}).get("m5") or {}
            buys, sells = int(m5.get("buys") or 0), int(m5.get("sells") or 0)
            chg = _f((p.get("priceChange") or {}).get("m5"))
            ok, code, detail = passes_filters(p, cfg, now_ms)
            tag = "WOULD BUY → rug check" if ok else f"filtered: {code} ({detail})"
            early = " [ultra-early]" if 0 <= age <= grace else ""
            log(
                f"probe: {sym:<10} {str(p.get('dexId', '?'))[:10]:<10} age={age:6.1f}s{early} "
                f"liq=${liq:8.0f} {buys}b/{sells}s m5={chg if chg is not None else '?'}% → {tag}"
            )
            results.append({"symbol": sym, "dex": p.get("dexId", "?"), "age_s": round(age, 1), "liq": liq, "tag": tag})
            if len(results) >= 25:
                log("probe: (first 25 shown)")
                return {"candidates": len(cands), "items": results}
    if not results:
        log("probe: 0 pairs found on DexScreener for current candidates.")
    return {"candidates": len(cands), "items": results}


def run(once=False, live=False):
    stream_mgr = RealtimeStreamManager(log_fn=log) if CONFIG.get("use_realtime_stream", True) else None
    if stream_mgr:
        stream_mgr.start(
            enable_pumpportal=CONFIG.get("use_pumpportal", True),
            enable_solana_ws=CONFIG.get("use_solana_ws", True),
        )
    if live or CONFIG.get("trading_mode") == "live":
        bridge = PhantomBrowserBridge(
            profile_dir=CONFIG.get("phantom_profile_dir", "phantom_profile"),
            extension_path=CONFIG.get("phantom_extension_path", ""),
            auto_confirm=CONFIG.get("phantom_auto_confirm", True),
            rpc_urls=RPC_URLS,
            jupiter_api_key=CONFIG.get("jupiter_api_key", ""),
            log_fn=log,
        )
        if not bridge.connected_once:
            bridge.connect_once()
        acct = LiveAccount(CONFIG, bridge, stream_mgr=stream_mgr)
        log("=== MULTI-SOURCE SNIPER STARTED (LIVE PHANTOM MODE) ===")
    else:
        acct = PaperAccount(CONFIG, stream_mgr=stream_mgr)
        log("=== MULTI-SOURCE SNIPER STARTED (PAPER MODE) ===")

    log(
        f"Sources: realtime_ws={CONFIG['use_realtime_stream']} gecko={CONFIG['use_gecko']} "
        f"jupiter={CONFIG['use_jupiter_recent']} profiles={CONFIG['use_dex_profiles']} "
        f"pumpportal={CONFIG['use_pumpportal']} boosts={CONFIG['use_promoted_feeds']}"
    )
    if once:
        scan_and_buy(acct, CONFIG, stream_mgr=stream_mgr)
        monitor(acct, stream_mgr=stream_mgr)
        if stream_mgr:
            stream_mgr.stop()
        return

    stop = threading.Event()

    def scan_loop():
        while not stop.is_set():
            started = time.time()
            try:
                scan_and_buy(acct, CONFIG, stream_mgr=stream_mgr)
            except Exception as e:
                log(f"scan error: {e}")
            stop.wait(max(0.1, CONFIG["scan_interval_sec"] - (time.time() - started)))

    threading.Thread(target=scan_loop, daemon=True).start()
    while True:
        started = time.time()
        try:
            monitor(acct, stream_mgr=stream_mgr)
        except KeyboardInterrupt:
            stop.set()
            if stream_mgr:
                stream_mgr.stop()
            break
        time.sleep(max(0.1, CONFIG["monitor_interval_sec"] - (time.time() - started)))


if __name__ == "__main__":
    if "--probe" in sys.argv:
        probe(CONFIG)
    else:
        run(once="--once" in sys.argv, live="--live" in sys.argv)
