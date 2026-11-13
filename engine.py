#!/usr/bin/env python3
"""
Shared pure-Python Sniper Engine, settings management, and account telemetry.
Used by both `gui.py` (Tkinter desktop GUI) and `web_dashboard.py` (Python web server).
"""
import csv
import json
import os
import threading
import time
from collections import deque
from datetime import datetime

import bot
from phantom_bridge import PhantomBrowserBridge, list_chrome_profiles
from realtime_stream import RealtimeStreamManager

SETTINGS_FILE = "settings.json"
SETTINGS = [
    ("trading_mode", "Trading mode ('paper' or 'live')", "str"),
    ("live_trade_size_sol", "Live trade size (SOL)", "float"),
    ("live_max_sol_per_trade", "Live safety cap (max SOL/trade)", "float"),
    ("gas_reserved_sol", "Gas reserve (SOL kept for tx fees)", "float"),
    ("live_dry_run", "Live dry-run (quote only, no Phantom tx)", "bool"),
    ("phantom_connection_mode", "Phantom mode ('existing_chrome', 'cdp_attach', 'isolated_profile')", "str"),
    ("phantom_chrome_profile", "Existing Chrome profile (e.g. Default, Profile 1)", "str"),
    ("phantom_cdp_port", "Chrome CDP port (for auto-confirm attach)", "int"),
    ("phantom_profile_dir", "Session/isolated profile folder", "str"),
    ("phantom_extension_path", "Phantom unpacked extension path (optional)", "str"),
    ("phantom_auto_confirm", "Auto-confirm Phantom swap popup", "bool"),
    ("start_balance_usd", "Start balance $ (used on reset)", "float"),
    ("trade_size_usd", "Paper trade size $", "float"),
    ("max_open_positions", "Max open positions", "int"),
    ("daily_loss_limit_usd", "Daily loss limit $", "float"),
    ("take_profit_pct", "Take profit % (0 = off)", "float"),
    ("trailing_stop_pct", "Trailing stop % from peak (0 = off)", "float"),
    ("max_hold_minutes", "Max hold (minutes)", "int"),
    ("max_age_minutes", "Sniper window: max coin age (minutes)", "float"),
    ("ultra_early_grace_sec", "Ultra-early grace window (sec)", "int"),
    ("scan_interval_sec", "Scan for new coins every (sec)", "int"),
    ("monitor_interval_sec", "Price check every (sec)", "float"),
    ("min_liquidity_usd", "Min liquidity $", "float"),
    ("min_buys_m5", "Min buys last 5 min", "int"),
    ("min_buy_sell_ratio_m5", "Min buy/sell ratio (5 min)", "float"),
    ("max_price_change_m5_pct", "Skip if pumped +X% in 5 min", "float"),
    ("min_volume_m5_usd", "Min 5-min volume $ (0 = off)", "float"),
    ("min_holders", "Min buys in last 1h (0 = off)", "int"),
    ("use_rugcheck", "Hard RugCheck (score + risks)", "bool"),
    ("strict_rugcheck", "Strict: reject ANY danger risk", "bool"),
    ("rugcheck_max_score", "Max RugCheck risk score (0-100)", "float"),
    ("max_top10_holder_pct", "Max top-10 holders %", "float"),
    ("rug_recheck_minutes", "Recheck rug score every (min)", "int"),
    ("use_realtime_stream", "Real-Time PumpPortal WS stream", "bool"),
    ("use_solana_ws", "Real-Time Solana RPC logsSubscribe WS", "bool"),
    ("use_gecko", "Source: GeckoTerminal new pools (all DEXes)", "bool"),
    ("use_jupiter_recent", "Source: Jupiter recent tokens", "bool"),
    ("use_jupiter_price", "Use Jupiter live prices", "bool"),
    ("use_dex_profiles", "Source: DexScreener token profiles", "bool"),
    ("use_pumpportal", "Source: PumpPortal new launches", "bool"),
    ("use_promoted_feeds", "Source: DexScreener boosted (promoted)", "bool"),
    ("slippage_pct", "Slippage %", "float"),
    ("fee_pct", "Simulated fee %", "float"),
    ("log_rejects", "Log filtered-out coins (debug)", "bool"),
]

LIMITS = {
    "live_trade_size_sol": (0.001, 50.0),
    "live_max_sol_per_trade": (0.001, 100.0),
    "gas_reserved_sol": (0.001, 1.0),
    "phantom_cdp_port": (1024, 65535),
    "start_balance_usd": (1, 1e7),
    "trade_size_usd": (0.5, 1e6),
    "max_open_positions": (1, 50),
    "daily_loss_limit_usd": (0, 1e7),
    "take_profit_pct": (0, 1e5),
    "trailing_stop_pct": (0, 1000),
    "max_hold_minutes": (1, 1e5),
    "max_age_minutes": (0.05, 60),
    "ultra_early_grace_sec": (0, 300),
    "scan_interval_sec": (1, 3600),
    "monitor_interval_sec": (0.2, 60),
    "min_liquidity_usd": (0, 1e9),
    "min_buys_m5": (0, 1e6),
    "min_buy_sell_ratio_m5": (0, 100),
    "max_price_change_m5_pct": (0, 1e6),
    "min_volume_m5_usd": (0, 1e9),
    "min_holders": (0, 1e6),
    "rugcheck_max_score": (0, 100),
    "max_top10_holder_pct": (0, 100),
    "rug_recheck_minutes": (1, 1440),
    "slippage_pct": (0, 50),
    "fee_pct": (0, 50),
}


def convert(value, kind):
    if kind == "bool":
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)
    if kind == "int":
        return int(float(value))
    if kind == "str":
        return str(value).strip()
    return float(value)


def parse_settings(raw):
    parsed, errors = {}, []
    for key, label, kind in SETTINGS:
        if key not in raw:
            parsed[key] = bot.CONFIG[key]
            continue
        try:
            val = convert(raw[key], kind)
        except (KeyError, ValueError, TypeError):
            errors.append(f"{label}: not a valid {kind}")
            continue
        if key == "trading_mode":
            val = val.lower()
            if val not in ("paper", "live"):
                errors.append(f"{label}: must be 'paper' or 'live'")
                continue
        elif key == "phantom_connection_mode":
            val = val.lower()
            if val not in ("existing_chrome", "cdp_attach", "isolated_profile"):
                errors.append(f"{label}: must be 'existing_chrome', 'cdp_attach', or 'isolated_profile'")
                continue
        elif key == "phantom_profile_dir" and not val:
            errors.append(f"{label}: cannot be empty")
            continue
        elif kind in ("int", "float"):
            lo, hi = LIMITS[key]
            if not (lo <= val <= hi):
                errors.append(f"{label}: must be between {lo:g} and {hi:g}")
                continue
        parsed[key] = val
    return parsed, errors


def load_settings():
    if not os.path.exists(SETTINGS_FILE):
        return
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        raw = {k: data.get(k, bot.CONFIG[k]) for k, _, _ in SETTINGS}
        parsed, errors = parse_settings(raw)
        if not errors:
            bot.CONFIG.update(parsed)
    except Exception:
        pass


def save_settings():
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump({k: bot.CONFIG[k] for k, _, _ in SETTINGS}, f, indent=2)


def money(x, sign=False):
    if x is None:
        return "..."
    s = f"${abs(x):,.2f}"
    if sign:
        return ("+" if x >= 0 else "-") + s
    return ("-" if x < 0 else "") + s


def pct(x):
    return "..." if x is None else f"{x:+.2f}%"


def fmt_held(minutes):
    if minutes < 60:
        return f"{minutes:.0f}m"
    return f"{int(minutes // 60)}h {int(minutes % 60):02d}m"


def position_rows(acct, prices):
    cfg = acct.cfg
    with acct.lock:
        items = [(m, dict(p)) for m, p in acct.state["positions"].items()]
    rows = []
    for mint, p in items:
        entry = p["entry_price"]
        price = prices.get(mint)
        chg = pnl_usd = value = None
        if price:
            chg = (price / entry - 1) * 100
            value = p["qty"] * price * (1 - cfg["slippage_pct"] / 100) * (1 - cfg["fee_pct"] / 100)
            pnl_usd = value - p["cost"]
        rows.append({
            "mint": mint,
            "symbol": p["symbol"],
            "entry": entry,
            "now": price,
            "chg": chg,
            "pnl_usd": pnl_usd,
            "value": value,
            "cost": p["cost"],
            "peak": (p.get("peak_price", entry) / entry - 1) * 100,
            "low": (p.get("low_price", entry) / entry - 1) * 100,
            "held": (time.time() - p["opened"]) / 60,
            "dex": p.get("dex", "?"),
            "source": p.get("source", "?"),
            "tx_sig": p.get("tx_sig", ""),
            "explorer_url": p.get("explorer_url", ""),
        })
    return rows


def account_stats(acct, rows):
    s = acct.state
    open_pnl = sum(r["pnl_usd"] for r in rows if r["pnl_usd"] is not None)
    open_value = sum(r["value"] if r["value"] is not None else r["cost"] for r in rows)
    wins, closed = s["wins"], s["closed"]
    return {
        "mode": getattr(acct, "mode", "paper"),
        "cash": s["cash"],
        "value": s["cash"] + open_value,
        "open_pnl": open_pnl,
        "day": s["day_pnl"],
        "total": s["total_pnl"],
        "wins": wins,
        "closed": closed,
        "winrate": f"{wins / closed * 100:.0f}%  ({wins}/{closed})" if closed else "no closed trades",
    }


def read_trades(path, limit=500):
    if not os.path.exists(path):
        return []
    out = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.reader(f):
            if len(row) < 9 or row[0] == "time":
                continue
            try:
                when = datetime.fromisoformat(row[0]).astimezone().strftime("%Y-%m-%d %H:%M:%S")
            except ValueError:
                when = row[0]
            usd = row[5]
            try:
                usd = f"${float(usd):,.2f}"
            except ValueError:
                pass
            pnl_usd = row[6]
            try:
                pnl_usd = money(float(pnl_usd), True)
            except ValueError:
                pass
            pnl_pct = f"{row[7]}%" if row[7] else ""
            out.append((when, row[1], row[2], "$" + row[4], usd, pnl_usd, pnl_pct, row[8], row[3]))
    return out[-limit:][::-1]


class SniperEngine:
    """
    Unified bot lifecycle engine managing:
    - PaperAccount / LiveAccount switching
    - PhantomBrowserBridge ("Browser Once" automation)
    - RealtimeStreamManager (PumpPortal WS + Solana RPC WS)
    - Scan & Price Monitor threads + Log ring buffer
    """

    def __init__(self, log_capacity=500):
        load_settings()
        self.logs = deque(maxlen=log_capacity)
        self._log_lock = threading.Lock()
        self._external_log_hooks = []

        self.stream_mgr = RealtimeStreamManager(log_fn=self._emit_log)
        self.bridge = PhantomBrowserBridge(
            profile_dir=bot.CONFIG.get("phantom_profile_dir", "phantom_profile"),
            extension_path=bot.CONFIG.get("phantom_extension_path", ""),
            auto_confirm=bot.CONFIG.get("phantom_auto_confirm", True),
            rpc_urls=bot.RPC_URLS,
            jupiter_api_key=bot.CONFIG.get("jupiter_api_key", ""),
            connection_mode=bot.CONFIG.get("phantom_connection_mode", "existing_chrome"),
            chrome_profile=bot.CONFIG.get("phantom_chrome_profile", "Default"),
            cdp_port=bot.CONFIG.get("phantom_cdp_port", 9222),
            gas_reserved_sol=bot.CONFIG.get("gas_reserved_sol", 0.02),
            log_fn=self._emit_log,
        )
        # Always start safely in Paper Mode until user explicitly unlocks Live Mode via ENABLE LIVE
        self.live_mode_confirmed = False
        self.mode = "paper"
        bot.CONFIG["trading_mode"] = "paper"
        self.paper_acct = bot.PaperAccount(bot.CONFIG, stream_mgr=self.stream_mgr)
        self.live_acct = bot.LiveAccount(bot.CONFIG, self.bridge, stream_mgr=self.stream_mgr)
        self.acct = self.paper_acct

        self.prices = {}
        self.last_price_time = 0.0
        self.running = False
        self._stop = None

        bot.LOG_HOOK = self._on_bot_log

    def add_log_hook(self, fn):
        self._external_log_hooks.append(fn)

    def _on_bot_log(self, line):
        with self._log_lock:
            self.logs.append(line)
        for fn in list(self._external_log_hooks):
            try:
                fn(line)
            except Exception:
                pass

    def _emit_log(self, msg):
        bot.log(msg)

    def get_logs(self, limit=200):
        with self._log_lock:
            items = list(self.logs)
        return items[-limit:]

    def validate_live_readiness(self):
        """Enforce all Phantom wallet, signing capability, and SOL + gas reserve safety checks."""
        if not self.bridge or not self.bridge.connected_once or not self.bridge.wallet_address:
            return False, "No Phantom wallet connected. Click 'Connect Phantom Wallet' first."
        if self.bridge.is_read_only or not self.bridge.signing_available:
            return False, "Connected wallet is read-only. Connect Phantom browser extension for signing."
        trade_sol = float(bot.CONFIG.get("live_trade_size_sol", 0.05))
        max_sol = float(bot.CONFIG.get("live_max_sol_per_trade", 0.25))
        gas_reserve = float(bot.CONFIG.get("gas_reserved_sol", 0.02))
        if trade_sol <= 0 or trade_sol > max_sol:
            return False, f"Live trade size ({trade_sol} SOL) exceeds safety cap ({max_sol} SOL)."
        if self.bridge.sol_balance < (trade_sol + gas_reserve):
            return (
                False,
                f"Insufficient SOL balance ({self.bridge.sol_balance:.4f} SOL). "
                f"Need at least {trade_sol + gas_reserve:.4f} SOL ({trade_sol:g} trade + {gas_reserve:g} gas reserve).",
            )
        return True, "ok"

    def confirm_enable_live_mode(self, confirmation_text=""):
        """
        Safety gate for switching into LIVE MODE:
        1) Stops any running bot first.
        2) Requires user to explicitly type ENABLE LIVE.
        3) Blocks if Phantom is disconnected, read-only, or below tradeAmountSol + gasReserveSol.
        """
        if self.running:
            self.stop_bot()

        normalized = " ".join(str(confirmation_text or "").strip().upper().split())
        if normalized != "ENABLE LIVE":
            return False, "Confirmation rejected: you must explicitly type ENABLE LIVE to unlock real trading."

        ok, why = self.validate_live_readiness()
        if not ok:
            return False, why

        self.live_mode_confirmed = True
        return self.set_mode("live", confirmed=True)

    def set_mode(self, mode, confirmed=False):
        mode = str(mode).lower().strip()
        if mode not in ("paper", "live"):
            return False, "Mode must be 'paper' or 'live'"
        if self.running:
            self.stop_bot()
        if mode == "live":
            if not (self.live_mode_confirmed or confirmed):
                return False, "Live Mode requires typing 'ENABLE LIVE' in the Live confirmation dialog."
            ok, why = self.validate_live_readiness()
            if not ok:
                self.live_mode_confirmed = False
                return False, why
            self.live_mode_confirmed = True
            self.mode = "live"
            bot.CONFIG["trading_mode"] = "live"
            save_settings()
            self.live_acct.sync_wallet_cash()
            self.acct = self.live_acct
            bot.log("Switched to LIVE MODE (Phantom Browser Popup execution armed).")
            return True, "Active mode: LIVE (Phantom confirmation active)"

        self.live_mode_confirmed = False
        self.mode = "paper"
        bot.CONFIG["trading_mode"] = "paper"
        save_settings()
        self.acct = self.paper_acct
        bot.log("Switched to PAPER MODE (safe simulated funds).")
        return True, "Active mode: PAPER"

    def connect_phantom_once(self, wallet_hint="", chrome_profile=None, connection_mode=None, dashboard_url=None):
        if chrome_profile:
            bot.CONFIG["phantom_chrome_profile"] = chrome_profile
        if connection_mode:
            bot.CONFIG["phantom_connection_mode"] = connection_mode
        if chrome_profile or connection_mode:
            save_settings()
        self.bridge.profile_dir = os.path.abspath(bot.CONFIG.get("phantom_profile_dir", "phantom_profile"))
        self.bridge.extension_path = bot.CONFIG.get("phantom_extension_path", "")
        self.bridge.auto_confirm = bool(bot.CONFIG.get("phantom_auto_confirm", True))
        self.bridge.connection_mode = bot.CONFIG.get("phantom_connection_mode", "existing_chrome")
        self.bridge.chrome_profile = bot.CONFIG.get("phantom_chrome_profile", "Default")
        self.bridge.cdp_port = int(bot.CONFIG.get("phantom_cdp_port", 9222))
        self.bridge.gas_reserved_sol = float(bot.CONFIG.get("gas_reserved_sol", 0.02))
        ok, msg = self.bridge.connect_once(wallet_hint=wallet_hint, dashboard_url=dashboard_url)
        if ok:
            self.live_acct.sync_wallet_cash()
        return ok, msg

    def disconnect_phantom(self):
        if self.running and self.mode == "live":
            self.stop_bot()
        self.live_mode_confirmed = False
        if self.mode == "live":
            self.set_mode("paper")
        self.bridge.disconnect_wallet()
        return True, "Phantom wallet disconnected and Live Mode disarmed."

    def close_phantom_browser(self):
        self.bridge.close()
        return True, "Phantom browser session closed (profile remains saved)."

    def start_stream_only(self):
        if not self.stream_mgr.is_running and bot.CONFIG.get("use_realtime_stream", True):
            self.stream_mgr.start(
                enable_pumpportal=bot.CONFIG.get("use_pumpportal", True),
                enable_solana_ws=bot.CONFIG.get("use_solana_ws", True),
            )

    def stop_stream(self):
        self.stream_mgr.stop()

    def start(self):
        if self.running:
            return False, "Already running"
        if self.mode == "live":
            if not self.live_mode_confirmed:
                return False, "Live Mode requires explicit confirmation ('ENABLE LIVE') first."
            ok, why = self.validate_live_readiness()
            if not ok:
                return False, why

        stop = threading.Event()
        self._stop = stop
        self.running = True
        self.start_stream_only()
        # Subscribe any open positions to real-time trade stream
        with self.acct.lock:
            for mint in self.acct.state["positions"]:
                self.stream_mgr.subscribe_token(mint)

        if self.mode == "live":
            bot.log("Bot started (LIVE PHANTOM MODE - real wallet swaps)")
        else:
            bot.log("Bot started (PAPER MODE - no real money)")

        threading.Thread(target=self._scan_loop, args=(stop,), daemon=True).start()
        threading.Thread(target=self._monitor_loop, args=(stop,), daemon=True).start()
        return True, f"Started in {self.mode.upper()} mode"

    def stop_bot(self):
        if self._stop:
            self._stop.set()
        if self.running:
            bot.log("Bot stopped.")
            self.running = False
            self.acct.save()
        self.stream_mgr.stop()
        return True, "Stopped"

    def shutdown(self):
        self.stop_bot()
        self.stream_mgr.stop()
        self.bridge.close()

    def _scan_loop(self, stop):
        while not stop.is_set():
            started = time.time()
            try:
                bot.scan_and_buy(self.acct, bot.CONFIG, stream_mgr=self.stream_mgr)
            except Exception as e:
                bot.log(f"scan error: {e}")
            stop.wait(max(0.1, bot.CONFIG["scan_interval_sec"] - (time.time() - started)))

    def _monitor_loop(self, stop):
        last_save = time.time()
        while not stop.is_set():
            started = time.time()
            try:
                prices = bot.monitor(self.acct, stream_mgr=self.stream_mgr)
                if prices:
                    self.prices.update(prices)
                    self.last_price_time = time.time()
                if time.time() - last_save >= 10:
                    self.acct.save()
                    last_save = time.time()
            except Exception as e:
                bot.log(f"monitor error: {e}")
            stop.wait(max(0.1, bot.CONFIG["monitor_interval_sec"] - (time.time() - started)))

    def sell_now(self, mint):
        with self.acct.lock:
            if mint not in self.acct.state["positions"]:
                return "That position is already closed."
            price = self.prices.get(mint) or self.stream_mgr.get_live_prices().get(mint)
            if not price:
                price = self.acct.state["positions"][mint]["entry_price"]
            self.acct.sell(mint, price, "manual")
            return f"Sold ({self.mode} trade)."

    def reset_account(self):
        if self.running:
            return False, "Stop the bot before resetting."
        if self.mode == "live":
            for path in (bot.CONFIG["live_state_file"], bot.CONFIG["live_trades_csv"]):
                if os.path.exists(path):
                    os.remove(path)
            self.prices.clear()
            self.live_acct = bot.LiveAccount(bot.CONFIG, self.bridge, stream_mgr=self.stream_mgr)
            self.acct = self.live_acct
            return True, "Live session log/positions cleared."
        for path in (bot.CONFIG["state_file"], bot.CONFIG["trades_csv"]):
            if os.path.exists(path):
                os.remove(path)
        self.prices.clear()
        self.paper_acct = bot.PaperAccount(bot.CONFIG, stream_mgr=self.stream_mgr)
        self.acct = self.paper_acct
        return True, "Paper account reset."

    def snapshot(self):
        rows = position_rows(self.acct, self.prices)
        stats = account_stats(self.acct, rows)
        csv_path = bot.CONFIG["live_trades_csv"] if self.mode == "live" else bot.CONFIG["trades_csv"]
        trades = read_trades(csv_path, limit=200)
        return {
            "running": self.running,
            "mode": self.mode,
            "live_mode_confirmed": self.live_mode_confirmed,
            "last_price_age_sec": round(time.time() - self.last_price_time, 1) if self.last_price_time else None,
            "stats": stats,
            "positions": rows,
            "trades": trades,
            "stream": self.stream_mgr.get_stats(),
            "stream_events": self.stream_mgr.get_recent_events(limit=60),
            "phantom": self.bridge.get_status(),
            "chrome_profiles": list_chrome_profiles(),
            "logs": self.get_logs(limit=150),
            "config": {k: bot.CONFIG[k] for k, _, _ in SETTINGS},
        }
