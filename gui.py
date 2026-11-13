#!/usr/bin/env python3
"""
Solana Launch Sniper Bot — Pure-Python Tkinter Desktop GUI.
Supports:
- Paper Mode (safe simulation) & Live Mode (Phantom Browser-Once automation)
- Real-Time WebSocket Data Streaming (PumpPortal WS + Solana RPC logsSubscribe)
"""
import queue
import sys
import threading
import time
import webbrowser

try:
    import tkinter as tk
    from tkinter import messagebox, ttk
except ImportError:
    tk = ttk = messagebox = None

import bot
from engine import (
    SETTINGS,
    SniperEngine,
    account_stats,
    fmt_held,
    money,
    parse_settings,
    pct,
    position_rows,
    read_trades,
    save_settings,
)

BG, CARD, LINE = "#f4f5f7", "#ffffff", "#dde1e6"
GREEN, RED, GREY, BLUE, ORANGE = "#0b7a3b", "#c62828", "#667085", "#1d4ed8", "#b45309"


class App:
    def __init__(self, root, engine, logq):
        self.root, self.engine, self.logq = root, engine, logq
        self.pos_items = set()
        self.csv_sig = None
        self.msg_job = None
        self._last_stream_sig = None

        root.title("Solana Launch Sniper - Pure Python (Paper & Live Phantom Mode)")
        root.geometry("1260x820")
        root.minsize(1040, 680)
        root.configure(bg=BG)

        self.build_style()
        self.build_header()
        self.build_cards()
        self.build_tabs()

        self.msg = tk.Label(root, text="", anchor="w", bg=BG, fg=GREY, font=("Segoe UI", 9))
        self.msg.pack(fill="x", padx=14, pady=(0, 6))
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.tick()

    def build_style(self):
        st = ttk.Style()
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure("TFrame", background=BG)
        st.configure("TNotebook", background=BG, borderwidth=0)
        st.configure("TNotebook.Tab", padding=(14, 7), font=("Segoe UI", 10))
        st.configure("Treeview", rowheight=26, font=("Segoe UI", 10), background=CARD, fieldbackground=CARD, bordercolor=LINE)
        st.configure("Treeview.Heading", font=("Segoe UI", 9, "bold"), padding=(6, 5))
        st.map("TNotebook.Tab", background=[("selected", CARD), ("!selected", "#e4e7ec")])
        st.configure("TButton", font=("Segoe UI", 10), padding=(12, 6), background="#e9ecf0")
        st.map("TButton", background=[("active", "#dde1e6")])
        st.configure("Start.TButton", background=GREEN, foreground="white", font=("Segoe UI", 11, "bold"), padding=(20, 8))
        st.map("Start.TButton", background=[("active", "#096532")])
        st.configure("Stop.TButton", background=RED, foreground="white", font=("Segoe UI", 11, "bold"), padding=(20, 8))
        st.map("Stop.TButton", background=[("active", "#a01f1f")])

    def build_header(self):
        bar = tk.Frame(self.root, bg=BG)
        bar.pack(fill="x", padx=14, pady=(12, 4))
        left = tk.Frame(bar, bg=BG)
        left.pack(side="left")
        tk.Label(left, text="Solana Launch Sniper - Live & Paper Engine", bg=BG, fg="#101828", font=("Segoe UI", 17, "bold")).pack(anchor="w")
        self.subtitle = tk.Label(
            left,
            text="All DEXes | Real-Time WS Streaming | Hard Rug Screen | Phantom Browser-Once Automation",
            bg=BG, fg=GREY, font=("Segoe UI", 9),
        )
        self.subtitle.pack(anchor="w")

        right = tk.Frame(bar, bg=BG)
        right.pack(side="right")

        self.mode_btn = ttk.Button(right, text="Mode: PAPER", command=self.toggle_mode)
        self.mode_btn.pack(side="left", padx=(0, 8))

        self.phantom_btn = ttk.Button(right, text="Connect Phantom (Once)", command=self.connect_phantom)
        self.phantom_btn.pack(side="left", padx=(0, 12))

        self.status = tk.Label(right, text="", bg=BG, font=("Segoe UI", 10, "bold"))
        self.status.pack(side="left", padx=(0, 14))

        self.btn = ttk.Button(right, text="Start bot", style="Start.TButton", command=self.toggle)
        self.btn.pack(side="left")

        # Secondary Real-time Stream + Phantom bar
        info_bar = tk.Frame(self.root, bg=CARD, highlightbackground=LINE, highlightthickness=1)
        info_bar.pack(fill="x", padx=14, pady=(2, 4))
        self.stream_lbl = tk.Label(info_bar, text="Stream: idle", bg=CARD, fg=GREY, font=("Segoe UI", 9, "bold"))
        self.stream_lbl.pack(side="left", padx=10, pady=5)
        self.phantom_lbl = tk.Label(info_bar, text="Phantom: disconnected", bg=CARD, fg=GREY, font=("Segoe UI", 9))
        self.phantom_lbl.pack(side="right", padx=10, pady=5)

    def build_cards(self):
        wrap = tk.Frame(self.root, bg=BG)
        wrap.pack(fill="x", padx=14, pady=6)
        self.cards = {}
        spec = [
            ("cash", "Cash / Wallet USD"),
            ("value", "Est. total value"),
            ("open_pnl", "Open PnL (est.)"),
            ("day", "Today's PnL"),
            ("total", "Realized PnL"),
            ("winrate", "Win rate"),
        ]
        for i, (key, caption) in enumerate(spec):
            wrap.columnconfigure(i, weight=1, uniform="card")
            box = tk.Frame(wrap, bg=CARD, highlightbackground=LINE, highlightthickness=1)
            box.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 8, 0))
            tk.Label(box, text=caption, bg=CARD, fg=GREY, font=("Segoe UI", 9)).pack(anchor="w", padx=12, pady=(8, 0))
            val = tk.Label(box, text="-", bg=CARD, fg="#101828", font=("Segoe UI", 15, "bold"))
            val.pack(anchor="w", padx=12, pady=(0, 8))
            self.cards[key] = val

    def tree(self, parent, columns, heads, widths, height=12):
        frame = tk.Frame(parent, bg=BG)
        tv = ttk.Treeview(frame, columns=columns, show="headings", height=height, selectmode="browse")
        for c, h, w in zip(columns, heads, widths):
            tv.heading(c, text=h)
            tv.column(c, width=w, anchor="w", stretch=(c == columns[-1]))
        sb = ttk.Scrollbar(frame, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=sb.set)
        tv.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        tv.tag_configure("up", foreground=GREEN)
        tv.tag_configure("down", foreground=RED)
        tv.tag_configure("flat", foreground="#101828")
        tv.tag_configure("buy", foreground=BLUE)
        return frame, tv

    def build_tabs(self):
        self.nb = nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True, padx=14, pady=(4, 6))

        # Tab 1: Open Positions
        t1 = ttk.Frame(nb)
        nb.add(t1, text="  Open positions  ")
        frame, self.pos_tree = self.tree(
            t1,
            ("coin", "pnl", "pnl_usd", "entry", "now", "peak", "low", "held", "key"),
            ("Coin", "PnL %", "PnL $ (est.)", "Entry price", "Price now", "Peak %", "Low %", "Held", "Coin key (token address)"),
            (95, 72, 90, 120, 120, 70, 70, 65, 400),
        )
        frame.pack(fill="both", expand=True, pady=(8, 4))
        self.pos_tree.bind("<Double-1>", lambda e: self.open_chart(self.pos_tree, 8))
        row = tk.Frame(t1, bg=BG)
        row.pack(fill="x", pady=(2, 6))
        ttk.Button(row, text="Copy coin key", command=lambda: self.copy_key(self.pos_tree, 8)).pack(side="left")
        ttk.Button(row, text="Open chart on DexScreener", command=lambda: self.open_chart(self.pos_tree, 8)).pack(side="left", padx=8)
        ttk.Button(row, text="Sell now", command=self.sell_selected).pack(side="left")
        self.pos_hint = tk.Label(row, text="", bg=BG, fg=GREY, font=("Segoe UI", 9))
        self.pos_hint.pack(side="right")

        # Tab 2: Live WebSocket Stream
        t_stream = ttk.Frame(nb)
        nb.add(t_stream, text="  Real-time stream  ")
        frame, self.stream_tree = self.tree(
            t_stream,
            ("time", "source", "kind", "symbol", "price", "mcap", "mint"),
            ("Time", "Stream", "Event", "Symbol", "Live Price $", "MCap $", "Mint / Signature"),
            (85, 115, 95, 95, 110, 110, 420),
        )
        frame.pack(fill="both", expand=True, pady=(8, 4))
        srow = tk.Frame(t_stream, bg=BG)
        srow.pack(fill="x", pady=(2, 6))
        ttk.Button(srow, text="Start WS Stream", command=self.engine.start_stream_only).pack(side="left")
        ttk.Button(srow, text="Stop WS Stream", command=self.engine.stop_stream).pack(side="left", padx=8)
        ttk.Button(srow, text="Run Probe", command=lambda: threading.Thread(target=bot.probe, args=(bot.CONFIG, self.engine.stream_mgr), daemon=True).start()).pack(side="left")

        # Tab 3: Trade History
        t2 = ttk.Frame(nb)
        nb.add(t2, text="  Trade history  ")
        frame, self.hist_tree = self.tree(
            t2,
            ("time", "action", "coin", "price", "usd", "pnl_usd", "pnl_pct", "reason", "key"),
            ("Time", "Action", "Coin", "Price", "Amount", "PnL $", "PnL %", "Exit reason", "Coin key"),
            (160, 75, 95, 120, 75, 80, 70, 110, 360),
        )
        frame.pack(fill="both", expand=True, pady=(8, 4))
        self.hist_tree.bind("<Double-1>", lambda e: self.open_chart(self.hist_tree, 8))
        row2 = tk.Frame(t2, bg=BG)
        row2.pack(fill="x", pady=(2, 6))
        ttk.Button(row2, text="Copy coin key", command=lambda: self.copy_key(self.hist_tree, 8)).pack(side="left")
        ttk.Button(row2, text="Open chart on DexScreener", command=lambda: self.open_chart(self.hist_tree, 8)).pack(side="left", padx=8)

        # Tab 4: Scan Log
        t3 = ttk.Frame(nb)
        nb.add(t3, text="  Scan log  ")
        box = tk.Frame(t3, bg=BG)
        box.pack(fill="both", expand=True, pady=8)
        self.log = tk.Text(box, wrap="none", font=("Consolas", 9), bg=CARD, relief="flat", state="disabled", highlightbackground=LINE, highlightthickness=1)
        sb = ttk.Scrollbar(box, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set)
        self.log.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        for tag, color in (("buy", GREEN), ("sell", BLUE), ("reject", GREY), ("warn", ORANGE)):
            self.log.tag_configure(tag, foreground=color)

        # Tab 5: Phantom & Live Automation
        t_ph = ttk.Frame(nb)
        nb.add(t_ph, text="  Phantom & Live  ")
        self.build_phantom_tab(t_ph)

        # Tab 6: Settings
        t4 = ttk.Frame(nb)
        nb.add(t4, text="  Settings  ")
        self.build_settings(t4)

    def build_phantom_tab(self, parent):
        box = tk.Frame(parent, bg=CARD, highlightbackground=LINE, highlightthickness=1)
        box.pack(fill="x", padx=10, pady=12)
        tk.Label(box, text="Phantom Wallet Browser-Once Automation", bg=CARD, fg="#101828", font=("Segoe UI", 13, "bold")).pack(anchor="w", padx=14, pady=(12, 4))
        tk.Label(
            box,
            text="1) Click 'Connect Phantom (Once)' to open the persistent browser profile (`phantom_profile/`).\n"
                 "2) Unlock Phantom and approve the connection ONCE. Your session persists for automated Live Mode swaps.\n"
                 "3) Switch Trading Mode to LIVE when ready. Enable 'Live dry-run' in Settings to test quotes without spending SOL.",
            bg=CARD, fg=GREY, justify="left", font=("Segoe UI", 9),
        ).pack(anchor="w", padx=14, pady=(0, 10))

        self.ph_detail = tk.Label(box, text="", bg=CARD, fg="#101828", justify="left", font=("Consolas", 9))
        self.ph_detail.pack(anchor="w", padx=14, pady=(0, 10))

        row = tk.Frame(box, bg=CARD)
        row.pack(fill="x", padx=14, pady=(0, 12))
        ttk.Button(row, text="Connect Phantom (Once)", command=self.connect_phantom).pack(side="left")
        ttk.Button(row, text="Refresh SOL Balance", command=self.refresh_phantom).pack(side="left", padx=8)
        ttk.Button(row, text="Close Browser Window", command=self.engine.close_phantom_browser).pack(side="left")

    def build_settings(self, parent):
        canvas = tk.Canvas(parent, bg=BG, highlightthickness=0)
        sb = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        inner = tk.Frame(canvas, bg=BG)
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=sb.set)
        canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        form = tk.Frame(inner, bg=BG)
        form.pack(fill="x", padx=10, pady=12)
        self.vars = {}
        half = (len(SETTINGS) + 1) // 2
        for i, (key, label, kind) in enumerate(SETTINGS):
            r, c = i % half, (i // half) * 3
            tk.Label(form, text=label, bg=BG, anchor="w", font=("Segoe UI", 10)).grid(row=r, column=c, sticky="w", padx=(0, 10), pady=4)
            if kind == "bool":
                var = tk.BooleanVar()
                ttk.Checkbutton(form, variable=var).grid(row=r, column=c + 1, sticky="w", pady=4)
            else:
                var = tk.StringVar()
                ttk.Entry(form, textvariable=var, width=18).grid(row=r, column=c + 1, sticky="w", pady=4)
            self.vars[key] = var
        form.columnconfigure(2, minsize=40)
        self.fill_settings()

        row = tk.Frame(inner, bg=BG)
        row.pack(fill="x", padx=10, pady=(4, 14))
        ttk.Button(row, text="Apply and save", command=self.apply_settings).pack(side="left")
        ttk.Button(row, text="Reset account...", command=self.reset_account).pack(side="left", padx=10)

    def fill_settings(self):
        for key, _, kind in SETTINGS:
            v = bot.CONFIG[key]
            if kind == "bool":
                self.vars[key].set(bool(v))
            elif kind == "str":
                self.vars[key].set(str(v))
            else:
                self.vars[key].set(f"{v:g}")

    def say(self, text, ms=4000):
        self.msg.config(text=text)
        if self.msg_job:
            self.root.after_cancel(self.msg_job)
        self.msg_job = self.root.after(ms, lambda: self.msg.config(text=""))

    def toggle(self):
        if self.engine.running:
            self.engine.stop_bot()
        else:
            ok, msg = self.engine.start()
            if not ok:
                messagebox.showwarning("Cannot start", msg)

    def toggle_mode(self):
        target = "live" if self.engine.mode == "paper" else "paper"
        if target == "live":
            if not messagebox.askyesno(
                "Switch to LIVE MODE",
                "LIVE MODE executes real swaps via your Phantom Browser-Once session.\n"
                f"Trade size: {bot.CONFIG['live_trade_size_sol']} SOL (max {bot.CONFIG['live_max_sol_per_trade']} SOL).\n\n"
                "Switch to LIVE MODE?",
            ):
                return
        ok, msg = self.engine.set_mode(target)
        if not ok:
            messagebox.showwarning("Mode switch", msg)
        self.say(msg)

    def connect_phantom(self):
        self.say("Opening persistent Phantom browser...")
        def worker():
            ok, msg = self.engine.connect_phantom_once()
            self.root.after(0, lambda: self.say(msg, 6000))
        threading.Thread(target=worker, daemon=True).start()

    def refresh_phantom(self):
        def worker():
            bal = self.engine.bridge.refresh_sol_balance()
            self.engine.live_acct.sync_wallet_cash()
            self.root.after(0, lambda: self.say(f"Wallet SOL balance: {bal:.4f} SOL"))
        threading.Thread(target=worker, daemon=True).start()

    def chosen_key(self, tv, col):
        sel = tv.selection()
        if not sel:
            self.say("Select a coin in the table first.")
            return None
        return tv.item(sel[0], "values")[col]

    def copy_key(self, tv, col):
        key = self.chosen_key(tv, col)
        if key:
            self.root.clipboard_clear()
            self.root.clipboard_append(key)
            self.say("Coin key copied.")

    def open_chart(self, tv, col):
        key = self.chosen_key(tv, col)
        if key:
            webbrowser.open(f"https://dexscreener.com/solana/{key}")

    def sell_selected(self):
        sel = self.pos_tree.selection()
        if not sel:
            self.say("Select an open position first.")
            return
        mint = sel[0]
        name = self.pos_tree.item(mint, "values")[0]
        if messagebox.askyesno("Sell now", f"Close the {self.engine.mode.upper()} position in {name} now?"):
            self.say(self.engine.sell_now(mint))

    def apply_settings(self):
        raw = {k: v.get() for k, v in self.vars.items()}
        parsed, errors = parse_settings(raw)
        if errors:
            messagebox.showerror("Check your settings", "\n".join(errors))
            return
        bot.CONFIG.update(parsed)
        save_settings()
        if parsed.get("trading_mode") != self.engine.mode and not self.engine.running:
            self.engine.set_mode(parsed["trading_mode"])
        self.fill_settings()
        self.say("Settings applied and saved.")

    def reset_account(self):
        if self.engine.running:
            messagebox.showinfo("Stop the bot first", "Press 'Stop bot', then reset the account.")
            return
        if messagebox.askyesno("Reset account", f"Clear {self.engine.mode.upper()} positions and trade history?"):
            ok, msg = self.engine.reset_account()
            self.csv_sig = None
            self.say(msg)

    def on_close(self):
        self.engine.shutdown()
        self.root.destroy()

    def tick(self):
        try:
            self.drain_log()
            rows = position_rows(self.engine.acct, self.engine.prices)
            self.update_positions(rows)
            self.update_cards(rows)
            self.update_status(rows)
            self.update_stream_ui()
            self.update_phantom_ui()
            self.update_history()
        except Exception as e:
            self.append_log(f"[window] refresh error: {e}")
        self.root.after(500, self.tick)

    def update_positions(self, rows):
        seen = set()
        for r in rows:
            mint = r["mint"]
            seen.add(mint)
            tag = "flat" if r["chg"] is None else ("up" if r["chg"] > 0 else "down" if r["chg"] < 0 else "flat")
            vals = (
                r["symbol"], pct(r["chg"]), money(r["pnl_usd"], True),
                bot.fmt_price(r["entry"]), bot.fmt_price(r["now"]) if r["now"] else "...",
                pct(r["peak"]), pct(r["low"]), fmt_held(r["held"]), mint,
            )
            if mint in self.pos_items:
                self.pos_tree.item(mint, values=vals, tags=(tag,))
            else:
                self.pos_tree.insert("", "end", iid=mint, values=vals, tags=(tag,))
                self.pos_items.add(mint)
        for mint in list(self.pos_items - seen):
            self.pos_tree.delete(mint)
            self.pos_items.discard(mint)
        cfg = bot.CONFIG
        self.pos_hint.config(text=f"{len(rows)} of {cfg['max_open_positions']} slots in use" if rows else "No open positions")

    def update_cards(self, rows):
        s = account_stats(self.engine.acct, rows)
        def paint(key, text, number=None):
            color = "#101828" if number is None else (GREEN if number > 0 else RED if number < 0 else "#101828")
            self.cards[key].config(text=text, fg=color)
        paint("cash", money(s["cash"]))
        paint("value", money(s["value"]))
        paint("open_pnl", money(s["open_pnl"], True), s["open_pnl"])
        paint("day", money(s["day"], True), s["day"])
        paint("total", money(s["total"], True), s["total"])
        paint("winrate", s["winrate"])

    def update_status(self, rows):
        eng = self.engine
        self.mode_btn.config(text=f"Mode: {eng.mode.upper()}")
        if not eng.running:
            self.status.config(text=f"●  STOPPED ({eng.mode.upper()})", fg=GREY)
            self.btn.config(text="Start bot", style="Start.TButton")
            return
        self.btn.config(text="Stop bot", style="Stop.TButton")
        age = time.time() - eng.last_price_time if eng.last_price_time else None
        if rows and (age is None or age > 15):
            self.status.config(text=f"●  RUNNING ({eng.mode.upper()}) - waiting for prices", fg=ORANGE)
        elif rows:
            self.status.config(text=f"●  RUNNING ({eng.mode.upper()}) - prices {age:.1f}s ago", fg=GREEN)
        else:
            self.status.config(text=f"●  RUNNING ({eng.mode.upper()}) - scanning {bot.CONFIG['scan_interval_sec']}s", fg=GREEN)

    def update_stream_ui(self):
        st = self.engine.stream_mgr.get_stats()
        color = GREEN if st["pump_status"] == "connected" else ORANGE if st["running"] else GREY
        self.stream_lbl.config(
            text=f"Real-Time Stream: PumpPortal={st['pump_status']} | SolanaWS={st['solana_status']} | {st['msgs_per_sec']} msg/s | SOL=${st['sol_price_usd']}",
            fg=color,
        )
        sig = (st["msg_count"], st["running"])
        if sig == self._last_stream_sig:
            return
        self._last_stream_sig = sig
        self.stream_tree.delete(*self.stream_tree.get_children())
        for ev in self.engine.stream_mgr.get_recent_events(limit=60):
            ts_str = time.strftime("%H:%M:%S", time.localtime(ev["ts"]))
            p_str = bot.fmt_price(ev["price_usd"]) if ev.get("price_usd") else "-"
            mc_str = f"${ev['market_cap_usd']:,.0f}" if ev.get("market_cap_usd") else "-"
            self.stream_tree.insert("", "end", values=(ts_str, ev["source"], ev["kind"], ev["symbol"], p_str, mc_str, ev["mint"]))

    def update_phantom_ui(self):
        ph = self.engine.bridge.get_status()
        addr = f"{ph['wallet_address'][:6]}...{ph['wallet_address'][-4:]}" if ph["wallet_address"] else "not connected"
        self.phantom_lbl.config(
            text=f"Phantom [{ph['status']}]: {addr} | {ph['sol_balance']:.4f} SOL | driver={ph['driver']}",
            fg=GREEN if ph["connected_once"] else GREY,
        )
        self.ph_detail.config(
            text=(
                f"Status:          {ph['status']}\n"
                f"Connected once:  {ph['connected_once']}\n"
                f"Wallet address:  {ph['wallet_address'] or '(none yet)'}\n"
                f"SOL balance:     {ph['sol_balance']:.6f} SOL\n"
                f"Browser driver:  {ph['driver']} (active={ph['browser_active']})\n"
                f"Profile dir:     {ph['profile_dir']}\n"
                f"Extension found: {ph['extension_detected']} ({ph['extension_path'] or 'auto'})\n"
                f"Last tx sig:     {ph['last_tx_sig'] or '-'}\n"
                f"Last message:    {ph['last_error'] or 'OK'}"
            )
        )

    def update_history(self):
        path = bot.CONFIG["live_trades_csv"] if self.engine.mode == "live" else bot.CONFIG["trades_csv"]
        try:
            st = os.stat(path)
            sig = (path, st.st_mtime, st.st_size)
        except OSError:
            sig = (path, None)
        if sig == self.csv_sig:
            return
        self.csv_sig = sig
        self.hist_tree.delete(*self.hist_tree.get_children())
        for vals in read_trades(path):
            if "BUY" in vals[1]:
                tag = "buy"
            else:
                tag = "up" if vals[5].startswith("+") else "down" if vals[5].startswith("-") else "flat"
            self.hist_tree.insert("", "end", values=vals, tags=(tag,))

    def drain_log(self):
        for _ in range(300):
            try:
                self.append_log(self.logq.get_nowait())
            except queue.Empty:
                break

    def append_log(self, line):
        low = line.lower()
        tag = (
            "reject" if ("reject" in low or "rug" in low or "filtered" in low)
            else "buy" if "sniper buy" in low
            else "sell" if "sell" in low
            else "warn" if any(w in low for w in ("error", "failed"))
            else None
        )
        at_bottom = self.log.yview()[1] > 0.97
        self.log.config(state="normal")
        self.log.insert("end", line + "\n", tag or ())
        lines = int(self.log.index("end-1c").split(".")[0])
        if lines > 3000:
            self.log.delete("1.0", f"{lines - 2500}.0")
        self.log.config(state="disabled")
        if at_bottom:
            self.log.see("end")


def main():
    if tk is None:
        print("tkinter is not available. Run `python3 web_dashboard.py` for the pure-Python web UI.")
        return
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    logq = queue.Queue()
    bot.CONSOLE_OUTPUT = False
    engine = SniperEngine()
    engine.add_log_hook(logq.put)
    root = tk.Tk()
    App(root, engine, logq)
    bot.log("Window ready. Real-time WS streaming + Phantom Browser-Once ready.")
    if "--start" in sys.argv:
        engine.start()
    root.mainloop()


if __name__ == "__main__":
    main()
