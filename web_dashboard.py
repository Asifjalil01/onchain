#!/usr/bin/env python3
"""
Pure-Python Web Control Center for the Solana Launch Sniper Bot.
Zero Node.js / React runtime required — served directly by Python stdlib
`ThreadingHTTPServer` with Server-Sent Events (`/api/events`) for real-time updates.

Usage:
    python3 web_dashboard.py --port 3000
"""
import argparse
import html
import json
import os
import signal
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import bot
from engine import (
    SETTINGS,
    SniperEngine,
    fmt_held,
    money,
    parse_settings,
    pct,
    save_settings,
)

_ENGINE = None
_ENGINE_LOCK = threading.Lock()


def get_engine():
    global _ENGINE
    with _ENGINE_LOCK:
        if _ENGINE is None:
            bot.CONSOLE_OUTPUT = True
            _ENGINE = SniperEngine(log_capacity=600)
            bot.log("Python Sniper Engine initialized (Paper + Live Phantom + Real-Time WS).")
        return _ENGINE


def _build_html():
    settings_schema = json.dumps(
        [{"key": k, "label": lbl, "kind": kind} for k, lbl, kind in SETTINGS]
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Solana Sniper Bot - Python Live &amp; Paper Engine</title>
  <style>
    :root {{
      --bg: #0b0f17;
      --surface: #121824;
      --surface-2: #182030;
      --border: #232d42;
      --text: #e6edf7;
      --muted: #8b9bb4;
      --green: #10b981;
      --green-bg: rgba(16, 185, 129, 0.14);
      --red: #ef4444;
      --red-bg: rgba(239, 68, 68, 0.14);
      --amber: #f59e0b;
      --amber-bg: rgba(245, 158, 11, 0.14);
      --blue: #38bdf8;
      --blue-bg: rgba(56, 189, 248, 0.14);
      --mono: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      --sans: Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: var(--sans);
      font-size: 14px;
      line-height: 1.45;
    }}
    .wrap {{
      max-width: 1380px;
      margin: 0 auto;
      padding: 16px 20px 28px;
    }}
    header {{
      display: flex;
      flex-wrap: wrap;
      justify-content: space-between;
      align-items: center;
      gap: 14px;
      padding-bottom: 14px;
      border-bottom: 1px solid var(--border);
    }}
    .brand h1 {{
      margin: 0;
      font-size: 20px;
      font-weight: 700;
      letter-spacing: -0.02em;
      display: flex;
      align-items: center;
      gap: 10px;
    }}
    .brand p {{
      margin: 4px 0 0;
      color: var(--muted);
      font-size: 12.5px;
    }}
    .badge {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 3px 9px;
      border-radius: 6px;
      font-size: 11px;
      font-weight: 700;
      letter-spacing: 0.04em;
      text-transform: uppercase;
      border: 1px solid transparent;
    }}
    .badge.paper {{ background: var(--blue-bg); color: var(--blue); border-color: rgba(56,189,248,0.3); }}
    .badge.live {{ background: var(--amber-bg); color: var(--amber); border-color: rgba(245,158,11,0.35); }}
    .badge.ok {{ background: var(--green-bg); color: var(--green); border-color: rgba(16,185,129,0.3); }}
    .badge.off {{ background: rgba(139,155,180,0.12); color: var(--muted); border-color: var(--border); }}
    .badge.err {{ background: var(--red-bg); color: var(--red); border-color: rgba(239,68,68,0.3); }}

    .controls {{
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 8px;
    }}
    button {{
      font-family: inherit;
      font-size: 13px;
      font-weight: 600;
      padding: 8px 14px;
      border-radius: 8px;
      border: 1px solid var(--border);
      background: var(--surface-2);
      color: var(--text);
      cursor: pointer;
      transition: background 0.15s, border-color 0.15s, transform 0.08s;
    }}
    button:hover {{ background: #202a3e; border-color: #354463; }}
    button:active {{ transform: translateY(1px); }}
    button:focus-visible, input:focus-visible {{
      outline: 2px solid var(--blue);
      outline-offset: 2px;
    }}
    button.primary-start {{ background: #059669; border-color: #10b981; color: #fff; padding: 9px 18px; }}
    button.primary-start:hover {{ background: #047857; }}
    button.primary-stop {{ background: #dc2626; border-color: #ef4444; color: #fff; padding: 9px 18px; }}
    button.primary-stop:hover {{ background: #b91c1c; }}
    button.accent {{ background: #1d4ed8; border-color: #3b82f6; color: #fff; }}
    button.accent:hover {{ background: #1e40af; }}
    button.warn {{ background: rgba(245, 158, 11, 0.15); border-color: rgba(245, 158, 11, 0.4); color: var(--amber); }}
    button.small {{ padding: 5px 10px; font-size: 12px; border-radius: 6px; }}

    .telemetry-strip {{
      margin-top: 12px;
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 9px 14px;
      display: flex;
      flex-wrap: wrap;
      justify-content: space-between;
      align-items: center;
      gap: 10px;
      font-size: 12.5px;
    }}
    .telemetry-group {{
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 12px;
    }}
    .dot {{
      width: 8px; height: 8px; border-radius: 50%; display: inline-block;
      background: var(--muted);
    }}
    .dot.live {{ background: var(--green); box-shadow: 0 0 8px var(--green); }}
    .dot.warn {{ background: var(--amber); }}

    .cards {{
      display: grid;
      grid-template-columns: repeat(6, minmax(0, 1fr));
      gap: 10px;
      margin-top: 12px;
    }}
    .card {{
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 12px 14px;
    }}
    .card .lbl {{ color: var(--muted); font-size: 12px; }}
    .card .val {{
      margin-top: 4px;
      font-size: 20px;
      font-weight: 700;
      font-variant-numeric: tabular-nums;
    }}
    .pos {{ color: var(--green); }}
    .neg {{ color: var(--red); }}

    .tabs {{
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      margin-top: 16px;
      border-bottom: 1px solid var(--border);
      padding-bottom: 8px;
    }}
    .tab-btn {{
      background: transparent;
      border: 1px solid transparent;
      color: var(--muted);
      padding: 7px 14px;
      border-radius: 7px;
    }}
    .tab-btn[aria-selected="true"] {{
      background: var(--surface-2);
      border-color: var(--border);
      color: var(--text);
    }}
    .panel {{
      display: none;
      margin-top: 12px;
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 14px;
    }}
    .panel.active {{ display: block; }}

    .table-wrap {{
      overflow-x: auto;
      max-height: 430px;
      overflow-y: auto;
      border: 1px solid var(--border);
      border-radius: 8px;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
      font-variant-numeric: tabular-nums;
    }}
    th, td {{
      padding: 8px 10px;
      text-align: left;
      border-bottom: 1px solid var(--border);
      white-space: nowrap;
    }}
    th {{
      background: var(--surface-2);
      color: var(--muted);
      font-size: 11.5px;
      text-transform: uppercase;
      letter-spacing: 0.04em;
      position: sticky;
      top: 0;
    }}
    tr:hover td {{ background: rgba(255,255,255,0.02); }}
    .mono {{ font-family: var(--mono); font-size: 12px; }}
    .empty-row {{ text-align: center; color: var(--muted); padding: 28px 12px; }}

    .toolbar {{
      display: flex;
      flex-wrap: wrap;
      justify-content: space-between;
      align-items: center;
      gap: 10px;
      margin-top: 10px;
    }}

    .log-box {{
      background: #080b11;
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 10px 12px;
      height: 430px;
      overflow-y: auto;
      font-family: var(--mono);
      font-size: 12px;
      line-height: 1.5;
    }}
    .log-line {{ white-space: pre-wrap; word-break: break-word; }}
    .log-buy {{ color: var(--green); }}
    .log-sell {{ color: var(--blue); }}
    .log-warn {{ color: var(--amber); }}
    .log-rej {{ color: var(--muted); }}

    .phantom-grid {{
      display: grid;
      grid-template-columns: 1.2fr 1fr;
      gap: 16px;
    }}
    .subcard {{
      background: var(--surface-2);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 14px;
    }}
    .subcard h2 {{ margin: 0 0 8px; font-size: 15px; }}
    .kv {{ display: grid; grid-template-columns: 150px 1fr; gap: 6px 10px; font-size: 12.5px; }}
    .kv .k {{ color: var(--muted); }}
    .kv .v {{ font-family: var(--mono); word-break: break-all; }}

    .field {{ margin-bottom: 10px; }}
    .field label {{ display: block; font-size: 12px; color: var(--muted); margin-bottom: 4px; }}
    .field input[type="text"], .field input[type="number"], .field select {{
      width: 100%;
      background: #0b0f17;
      color: var(--text);
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 7px 10px;
      font-size: 13px;
    }}

    .settings-grid {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 10px 16px;
    }}
    .check-field {{
      display: flex;
      align-items: center;
      gap: 8px;
      padding-top: 18px;
    }}
    .check-field label {{ margin: 0; color: var(--text); font-size: 13px; cursor: pointer; }}
    .alert-box {{
      margin-top: 10px;
      padding: 10px 12px;
      border-radius: 8px;
      font-size: 12.5px;
      display: none;
    }}
    .alert-box.show {{ display: block; }}
    .alert-box.err {{ background: var(--red-bg); border: 1px solid rgba(239,68,68,0.4); color: #fecaca; }}
    .alert-box.ok {{ background: var(--green-bg); border: 1px solid rgba(16,185,129,0.4); color: #a7f3d0; }}

    .toast {{
      margin-top: 10px;
      min-height: 20px;
      color: var(--blue);
      font-size: 12.5px;
    }}

    dialog {{
      background: var(--surface);
      color: var(--text);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 18px 20px;
      max-width: 440px;
      width: calc(100% - 32px);
    }}
    dialog::backdrop {{ background: rgba(0, 0, 0, 0.65); }}
    dialog h3 {{ margin: 0 0 8px; font-size: 16px; }}
    dialog p {{ margin: 0 0 16px; color: var(--muted); font-size: 13px; }}
    .dialog-actions {{ display: flex; justify-content: flex-end; gap: 8px; }}

    @media (max-width: 1080px) {{
      .cards {{ grid-template-columns: repeat(3, minmax(0, 1fr)); }}
      .settings-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      .phantom-grid {{ grid-template-columns: 1fr; }}
    }}
    @media (max-width: 640px) {{
      .cards {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      .settings-grid {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <header>
      <div class="brand">
        <h1>
          Solana Sniper Bot
          <span id="mode-badge" class="badge paper">PAPER MODE</span>
          <span id="run-badge" class="badge off">STOPPED</span>
        </h1>
        <p>100% Python Engine &middot; Multi-Source Launch Sniper &middot; Real-Time WebSockets &middot; Phantom Browser-Once Automation</p>
      </div>
      <div class="controls">
        <button id="btn-mode" type="button" onclick="requestModeToggle()">Switch to Live Mode</button>
        <button id="btn-phantom-top" type="button" class="accent" onclick="connectPhantomOnce()">Connect Phantom (Once)</button>
        <button id="btn-start-stop" type="button" class="primary-start" onclick="toggleBot()">Start Bot</button>
      </div>
    </header>

    <section class="telemetry-strip" aria-label="Real-time telemetry">
      <div class="telemetry-group">
        <span><span id="ws-dot" class="dot"></span> <strong>Stream:</strong> <span id="ws-summary">disconnected</span></span>
        <span><strong>SOL:</strong> <span id="sol-price">$180.00</span></span>
        <span><strong>Rate:</strong> <span id="ws-rate">0 msg/s</span></span>
      </div>
      <div class="telemetry-group">
        <span><strong>Phantom:</strong> <span id="phantom-top-status">disconnected</span></span>
        <span><strong>Wallet:</strong> <span id="phantom-top-addr" class="mono">none</span></span>
        <span><strong>Balance:</strong> <span id="phantom-top-sol">0.0000 SOL</span></span>
      </div>
    </section>

    <section class="cards" aria-label="Account summary">
      <div class="card"><div class="lbl">Cash / Wallet USD</div><div id="card-cash" class="val">$0.00</div></div>
      <div class="card"><div class="lbl">Est. Total Value</div><div id="card-value" class="val">$0.00</div></div>
      <div class="card"><div class="lbl">Open PnL (est.)</div><div id="card-open-pnl" class="val">$0.00</div></div>
      <div class="card"><div class="lbl">Today's PnL</div><div id="card-day" class="val">$0.00</div></div>
      <div class="card"><div class="lbl">Realized PnL</div><div id="card-total" class="val">$0.00</div></div>
      <div class="card"><div class="lbl">Win Rate</div><div id="card-winrate" class="val">-</div></div>
    </section>

    <nav class="tabs" role="tablist" aria-label="Bot workspace">
      <button class="tab-btn" role="tab" aria-selected="true" data-tab="positions" onclick="selectTab('positions')">Open Positions (<span id="pos-count">0</span>)</button>
      <button class="tab-btn" role="tab" aria-selected="false" data-tab="stream" onclick="selectTab('stream')">Real-Time Stream</button>
      <button class="tab-btn" role="tab" aria-selected="false" data-tab="phantom" onclick="selectTab('phantom')">Phantom &amp; Live</button>
      <button class="tab-btn" role="tab" aria-selected="false" data-tab="history" onclick="selectTab('history')">Trade History</button>
      <button class="tab-btn" role="tab" aria-selected="false" data-tab="logs" onclick="selectTab('logs')">Scan Log</button>
      <button class="tab-btn" role="tab" aria-selected="false" data-tab="settings" onclick="selectTab('settings')">Settings</button>
    </nav>

    <main>
      <section id="tab-positions" class="panel active" role="tabpanel">
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Coin</th><th>PnL %</th><th>PnL $</th><th>Entry</th><th>Now</th>
                <th>Peak %</th><th>Low %</th><th>Held</th><th>DEX / Src</th><th>Mint</th><th>Action</th>
              </tr>
            </thead>
            <tbody id="pos-body">
              <tr><td colspan="11" class="empty-row">No open positions</td></tr>
            </tbody>
          </table>
        </div>
      </section>

      <section id="tab-stream" class="panel" role="tabpanel">
        <div class="toolbar" style="margin-top:0; margin-bottom:10px;">
          <div>
            <strong>Live WebSocket Feed</strong>
            <span style="color:var(--muted); font-size:12px; margin-left:8px;">PumpPortal WS (new launches + trade ticks) &amp; Solana RPC logsSubscribe</span>
          </div>
          <div class="controls">
            <button type="button" class="small" onclick="postAction('/api/stream/start')">Start WS Stream</button>
            <button type="button" class="small" onclick="postAction('/api/stream/stop')">Stop WS Stream</button>
            <button type="button" class="small" onclick="postAction('/api/bot/probe')">Run Probe</button>
          </div>
        </div>
        <div class="table-wrap">
          <table>
            <thead>
              <tr><th>Time</th><th>Stream</th><th>Event</th><th>Symbol</th><th>Price USD</th><th>MCap USD</th><th>Mint / Signature</th></tr>
            </thead>
            <tbody id="stream-body">
              <tr><td colspan="7" class="empty-row">Waiting for real-time stream events...</td></tr>
            </tbody>
          </table>
        </div>
      </section>

      <section id="tab-phantom" class="panel" role="tabpanel">
        <div class="phantom-grid">
          <div class="subcard">
            <h2>Browser-Once Phantom Setup (Existing Chrome)</h2>
            <p style="color:var(--muted); font-size:12.5px; margin-top:0;">
              Connects to your already-open Chrome browser &amp; profile where Phantom is unlocked (no brand-new profile or external redirect). Keep the local bridge tab open for Live Mode swaps.
            </p>
            <div class="field">
              <label for="ph-conn-mode">Connection Mode</label>
              <select id="ph-conn-mode">
                <option value="existing_chrome">existing_chrome (Use Already-Open Chrome &amp; Profile)</option>
                <option value="cdp_attach">cdp_attach (Attach to Chrome --remote-debugging-port)</option>
                <option value="isolated_profile">isolated_profile (Separate phantom_profile/)</option>
              </select>
            </div>
            <div class="field">
              <label for="ph-chrome-profile">Existing Chrome Profile</label>
              <select id="ph-chrome-profile">
                <option value="Default">Default</option>
              </select>
            </div>
            <div class="field">
              <label for="ph-wallet-hint">Wallet Public Key (auto-detected from Phantom, or enter manually for RPC/quote binding)</label>
              <input id="ph-wallet-hint" type="text" placeholder="Optional Solana address (base58)" />
            </div>
            <div class="controls">
              <button type="button" class="accent" onclick="connectPhantomOnce()">Connect Phantom (Once)</button>
              <button type="button" onclick="postAction('/api/phantom/refresh')">Refresh SOL Balance</button>
              <button type="button" onclick="postAction('/api/phantom/close')">Close Bridge</button>
            </div>
          </div>
          <div class="subcard">
            <h2>Phantom Bridge Diagnostics</h2>
            <div class="kv" id="phantom-kv"></div>
          </div>
        </div>
      </section>

      <section id="tab-history" class="panel" role="tabpanel">
        <div class="table-wrap">
          <table>
            <thead>
              <tr><th>Time</th><th>Action</th><th>Coin</th><th>Price</th><th>Amount</th><th>PnL $</th><th>PnL %</th><th>Reason</th><th>Mint</th></tr>
            </thead>
            <tbody id="hist-body">
              <tr><td colspan="9" class="empty-row">No recorded trades yet</td></tr>
            </tbody>
          </table>
        </div>
      </section>

      <section id="tab-logs" class="panel" role="tabpanel">
        <div id="log-box" class="log-box" aria-live="polite"></div>
      </section>

      <section id="tab-settings" class="panel" role="tabpanel">
        <form id="settings-form" onsubmit="saveSettingsForm(event)">
          <div id="settings-fields" class="settings-grid"></div>
          <div id="settings-alert" class="alert-box" role="alert"></div>
          <div class="toolbar">
            <div class="controls">
              <button type="submit" class="accent">Apply and Save Settings</button>
              <button type="button" class="warn" onclick="requestReset()">Reset Account...</button>
            </div>
          </div>
        </form>
      </section>
    </main>

    <div id="toast" class="toast" role="status" aria-live="polite"></div>
  </div>

  <dialog id="confirm-dialog" aria-labelledby="dlg-title">
    <h3 id="dlg-title">Confirm action</h3>
    <p id="dlg-body"></p>
    <div class="dialog-actions">
      <button type="button" onclick="closeDialog()">Cancel</button>
      <button id="dlg-confirm-btn" type="button" class="warn">Confirm</button>
    </div>
  </dialog>

  <script>
    const SETTINGS_SCHEMA = {settings_schema};
    let latestState = null;
    let settingsLoadedOnce = false;
    let pendingDialogAction = null;

    function selectTab(name) {{
      document.querySelectorAll('.tab-btn').forEach(btn => {{
        const active = btn.dataset.tab === name;
        btn.setAttribute('aria-selected', active ? 'true' : 'false');
      }});
      document.querySelectorAll('.panel').forEach(p => {{
        p.classList.toggle('active', p.id === 'tab-' + name);
      }});
    }}

    function showToast(msg, isError = false) {{
      const el = document.getElementById('toast');
      el.style.color = isError ? 'var(--red)' : 'var(--blue)';
      el.textContent = msg || '';
    }}

    function openDialog(title, body, onConfirm) {{
      document.getElementById('dlg-title').textContent = title;
      document.getElementById('dlg-body').textContent = body;
      pendingDialogAction = onConfirm;
      document.getElementById('confirm-dialog').showModal();
    }}
    function closeDialog() {{
      pendingDialogAction = null;
      document.getElementById('confirm-dialog').close();
    }}
    document.getElementById('dlg-confirm-btn').addEventListener('click', () => {{
      const fn = pendingDialogAction;
      closeDialog();
      if (fn) fn();
    }});

    async function postAction(url, body = {{}}) {{
      try {{
        const res = await fetch(url, {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify(body),
        }});
        const data = await res.json();
        showToast(data.message || (data.ok ? 'Done' : 'Error'), !data.ok);
        await refreshState();
        return data;
      }} catch (e) {{
        showToast('Request failed: ' + e.message, true);
        return {{ ok: false }};
      }}
    }}

    function toggleBot() {{
      if (latestState && latestState.running) postAction('/api/bot/stop');
      else postAction('/api/bot/start');
    }}

    function requestModeToggle() {{
      const nextMode = (latestState && latestState.mode === 'live') ? 'paper' : 'live';
      if (nextMode === 'live') {{
        openDialog(
          'Switch to LIVE MODE?',
          'Live Mode executes swaps through your Phantom Browser-Once session. Ensure your trade size (SOL) and live dry-run settings are configured properly.',
          () => postAction('/api/mode', {{ mode: 'live' }})
        );
      }} else {{
        postAction('/api/mode', {{ mode: 'paper' }});
      }}
    }}

    function connectPhantomOnce() {{
      const hint = (document.getElementById('ph-wallet-hint').value || '').trim();
      const modeEl = document.getElementById('ph-conn-mode');
      const profEl = document.getElementById('ph-chrome-profile');
      const connection_mode = modeEl ? modeEl.value : 'existing_chrome';
      const chrome_profile = profEl ? profEl.value : 'Default';
      showToast(`Opening Phantom bridge in Chrome (${{chrome_profile}})...`);
      postAction('/api/phantom/connect', {{ wallet_hint: hint, connection_mode, chrome_profile }});
    }}

    function requestReset() {{
      openDialog(
        'Reset Account State?',
        'This deletes open positions and trade history CSV for the currently active trading mode.',
        () => postAction('/api/account/reset')
      );
    }}

    function sellPosition(mint, sym) {{
      openDialog(
        'Close Position Now?',
        `Close ${{sym}} (${{mint.slice(0, 8)}}...) at current price?`,
        () => postAction('/api/positions/sell', {{ mint }})
      );
    }}

    function fmtMoney(n, signed = false) {{
      if (n === null || n === undefined) return '...';
      const abs = Math.abs(n).toLocaleString('en-US', {{ minimumFractionDigits: 2, maximumFractionDigits: 2 }});
      if (signed) return (n >= 0 ? '+' : '-') + '$' + abs;
      return (n < 0 ? '-' : '') + '$' + abs;
    }}
    function fmtPrice(x) {{
      if (x === null || x === undefined) return '...';
      if (Math.abs(x) >= 1) return '$' + Number(x).toFixed(4);
      const dec = Math.min(10, Math.max(2, -Math.floor(Math.log10(Math.abs(x))) + 5));
      return '$' + Number(x).toFixed(dec);
    }}
    function esc(s) {{
      return String(s ?? '').replace(/[&<>"']/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));
    }}

    function buildSettingsForm(cfg) {{
      const wrap = document.getElementById('settings-fields');
      wrap.innerHTML = '';
      for (const item of SETTINGS_SCHEMA) {{
        const val = cfg[item.key];
        const div = document.createElement('div');
        if (item.kind === 'bool') {{
          div.className = 'field check-field';
          div.innerHTML = `<input type="checkbox" id="set-${{item.key}}" name="${{item.key}}" ${{val ? 'checked' : ''}} /><label for="set-${{item.key}}">${{esc(item.label)}}</label>`;
        }} else {{
          div.className = 'field';
          const inputType = (item.kind === 'int' || item.kind === 'float') ? 'number' : 'text';
          const step = item.kind === 'float' ? 'any' : (item.kind === 'int' ? '1' : '');
          div.innerHTML = `<label for="set-${{item.key}}">${{esc(item.label)}}</label><input id="set-${{item.key}}" name="${{item.key}}" type="${{inputType}}" ${{step ? `step="${{step}}"` : ''}} value="${{esc(val)}}" />`;
        }}
        wrap.appendChild(div);
      }}
      settingsLoadedOnce = true;
    }}

    async function saveSettingsForm(e) {{
      e.preventDefault();
      const payload = {{}};
      for (const item of SETTINGS_SCHEMA) {{
        const el = document.getElementById('set-' + item.key);
        if (!el) continue;
        payload[item.key] = item.kind === 'bool' ? el.checked : el.value;
      }}
      const alertBox = document.getElementById('settings-alert');
      alertBox.className = 'alert-box';
      const res = await fetch('/api/settings', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify(payload),
      }});
      const data = await res.json();
      if (!data.ok) {{
        alertBox.className = 'alert-box show err';
        alertBox.textContent = (data.errors || [data.message || 'Invalid settings']).join(' | ');
      }} else {{
        alertBox.className = 'alert-box show ok';
        alertBox.textContent = data.message || 'Settings saved.';
        settingsLoadedOnce = false;
        await refreshState();
      }}
    }}

    function renderState(s) {{
      latestState = s;
      const isLive = s.mode === 'live';
      const modeBadge = document.getElementById('mode-badge');
      modeBadge.textContent = isLive ? 'LIVE PHANTOM MODE' : 'PAPER MODE';
      modeBadge.className = 'badge ' + (isLive ? 'live' : 'paper');

      const runBadge = document.getElementById('run-badge');
      runBadge.textContent = s.running ? 'RUNNING' : 'STOPPED';
      runBadge.className = 'badge ' + (s.running ? 'ok' : 'off');

      document.getElementById('btn-mode').textContent = isLive ? 'Switch to Paper Mode' : 'Switch to Live Mode';
      const startBtn = document.getElementById('btn-start-stop');
      startBtn.textContent = s.running ? 'Stop Bot' : 'Start Bot';
      startBtn.className = s.running ? 'primary-stop' : 'primary-start';

      // Stream telemetry
      const st = s.stream || {{}};
      const wsDot = document.getElementById('ws-dot');
      wsDot.className = 'dot ' + (st.pump_status === 'connected' ? 'live' : (st.running ? 'warn' : ''));
      document.getElementById('ws-summary').textContent = st.running
        ? `PumpPortal=${{st.pump_status}} · SolanaWS=${{st.solana_status}}`
        : 'idle (starts with bot)';
      document.getElementById('sol-price').textContent = '$' + Number(st.sol_price_usd || 180).toFixed(2);
      document.getElementById('ws-rate').textContent = `${{st.msgs_per_sec || 0}} msg/s (${{st.msg_count || 0}} total)`;

      // Phantom telemetry
      const ph = s.phantom || {{}};
      document.getElementById('phantom-top-status').textContent = ph.status || 'disconnected';
      document.getElementById('phantom-top-addr').textContent = ph.wallet_address
        ? `${{ph.wallet_address.slice(0, 6)}}...${{ph.wallet_address.slice(-4)}}`
        : 'not connected';
      document.getElementById('phantom-top-sol').textContent = `${{Number(ph.sol_balance || 0).toFixed(4)}} SOL`;

      // Cards
      const stats = s.stats || {{}};
      document.getElementById('card-cash').textContent = fmtMoney(stats.cash);
      document.getElementById('card-value').textContent = fmtMoney(stats.value);
      const paint = (id, val) => {{
        const el = document.getElementById(id);
        el.textContent = fmtMoney(val, true);
        el.className = 'val ' + (val > 0 ? 'pos' : val < 0 ? 'neg' : '');
      }};
      paint('card-open-pnl', stats.open_pnl || 0);
      paint('card-day', stats.day || 0);
      paint('card-total', stats.total || 0);
      document.getElementById('card-winrate').textContent = stats.winrate || '-';

      // Open Positions
      const pos = s.positions || [];
      document.getElementById('pos-count').textContent = pos.length;
      const posBody = document.getElementById('pos-body');
      posBody.innerHTML = pos.length ? pos.map(r => {{
        const cls = r.chg > 0 ? 'pos' : r.chg < 0 ? 'neg' : '';
        return `<tr>
          <td><strong>${{esc(r.symbol)}}</strong></td>
          <td class="${{cls}}">${{r.chg == null ? '...' : (r.chg >= 0 ? '+' : '') + r.chg.toFixed(2) + '%'}}</td>
          <td class="${{cls}}">${{r.pnl_usd == null ? '...' : fmtMoney(r.pnl_usd, true)}}</td>
          <td>${{fmtPrice(r.entry)}}</td>
          <td>${{fmtPrice(r.now)}}</td>
          <td>+${{Number(r.peak || 0).toFixed(1)}}%</td>
          <td>${{Number(r.low || 0).toFixed(1)}}%</td>
          <td>${{Number(r.held || 0).toFixed(0)}}m</td>
          <td>${{esc(r.dex)}} / ${{esc(r.source)}}</td>
          <td class="mono">${{esc(r.mint)}}</td>
          <td><button type="button" class="small" onclick="sellPosition('${{esc(r.mint)}}', '${{esc(r.symbol)}}')">Sell</button></td>
        </tr>`;
      }}).join('') : '<tr><td colspan="11" class="empty-row">No open positions</td></tr>';

      // Stream Events
      const evs = s.stream_events || [];
      const stBody = document.getElementById('stream-body');
      stBody.innerHTML = evs.length ? evs.map(ev => {{
        const t = new Date(ev.ts * 1000).toLocaleTimeString();
        return `<tr>
          <td>${{esc(t)}}</td>
          <td>${{esc(ev.source)}}</td>
          <td>${{esc(ev.kind)}}</td>
          <td><strong>${{esc(ev.symbol)}}</strong></td>
          <td>${{ev.price_usd ? fmtPrice(ev.price_usd) : '-'}}</td>
          <td>${{ev.market_cap_usd ? fmtMoney(ev.market_cap_usd) : '-'}}</td>
          <td class="mono">${{esc(ev.mint)}}</td>
        </tr>`;
      }}).join('') : '<tr><td colspan="7" class="empty-row">Waiting for real-time stream events...</td></tr>';

      // Phantom Diagnostics + Profile Options
      const profSelect = document.getElementById('ph-chrome-profile');
      if (profSelect && Array.isArray(s.chrome_profiles) && profSelect.options.length <= 1) {{
        const curVal = ph.chrome_profile || 'Default';
        profSelect.innerHTML = s.chrome_profiles.map(p =>
          `<option value="${{esc(p.dir)}}">${{esc(p.name)}} (${{esc(p.dir)}})</option>`
        ).join('');
        profSelect.value = curVal;
      }}
      const modeSelect = document.getElementById('ph-conn-mode');
      if (modeSelect && ph.connection_mode && document.activeElement !== modeSelect) {{
        modeSelect.value = ph.connection_mode;
      }}
      document.getElementById('phantom-kv').innerHTML = [
        ['Status', ph.status],
        ['Connected Once', String(ph.connected_once)],
        ['Wallet Address', ph.wallet_address || '(none)'],
        ['SOL Balance', `${{Number(ph.sol_balance || 0).toFixed(6)}} SOL`],
        ['Connection Mode', `${{ph.connection_mode}} (profile=${{ph.chrome_profile}})`],
        ['Driver', `${{ph.driver}} (relay_alive=${{ph.relay_tab_alive}}, active=${{ph.browser_active}})`],
        ['Relay URL', ph.relay_url || '(starts on connect)'],
        ['Extension Found', `${{ph.extension_detected}} (${{ph.extension_path || 'auto'}})`],
        ['Chromium Found', `${{ph.chromium_detected}} (${{ph.chromium_path || 'not found'}})`],
        ['Last Tx Sig', ph.last_tx_sig || '-'],
        ['Last Message', ph.last_error || 'OK'],
      ].map(([k, v]) => `<div class="k">${{esc(k)}}</div><div class="v">${{esc(v)}}</div>`).join('');

      // History
      const trades = s.trades || [];
      const hBody = document.getElementById('hist-body');
      hBody.innerHTML = trades.length ? trades.map(row => {{
        const actCls = String(row[1]).includes('BUY') ? 'log-buy' : '';
        return `<tr>${{row.map((cell, i) => `<td class="${{i === 1 ? actCls : (i === 8 ? 'mono' : '')}}">${{esc(cell)}}</td>`).join('')}}</tr>`;
      }}).join('') : '<tr><td colspan="9" class="empty-row">No recorded trades yet</td></tr>';

      // Logs
      const logBox = document.getElementById('log-box');
      const atBottom = logBox.scrollHeight - logBox.scrollTop - logBox.clientHeight < 40;
      logBox.innerHTML = (s.logs || []).map(line => {{
        const low = line.toLowerCase();
        const cls = (low.includes('sniper buy') || low.includes('live buy')) ? 'log-buy'
          : low.includes('sell') ? 'log-sell'
          : (low.includes('error') || low.includes('failed')) ? 'log-warn'
          : (low.includes('reject') || low.includes('filtered') || low.includes('rug')) ? 'log-rej' : '';
        return `<div class="log-line ${{cls}}">${{esc(line)}}</div>`;
      }}).join('');
      if (atBottom) logBox.scrollTop = logBox.scrollHeight;

      if (!settingsLoadedOnce && s.config) {{
        buildSettingsForm(s.config);
      }}
    }}

    async function refreshState() {{
      const res = await fetch('/api/state');
      if (res.ok) renderState(await res.json());
    }}

    refreshState();
    const evtSource = new EventSource('/api/events');
    evtSource.onmessage = (e) => {{
      try {{ renderState(JSON.parse(e.data)); }} catch {{}}
    }};
  </script>
</body>
</html>"""


class DashboardHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        # Suppress noisy per-request stderr logs
        pass

    def _send_json(self, payload, status=200):
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def do_GET(self):
        path = urlparse(self.path).path
        eng = get_engine()

        if path in ("/", "/index.html"):
            body = _build_html().encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/api/state":
            self._send_json(eng.snapshot())
            return

        if path == "/api/events":
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            try:
                while True:
                    data = json.dumps(eng.snapshot(), default=str)
                    msg = f"data: {data}\n\n".encode("utf-8")
                    self.wfile.write(msg)
                    self.wfile.flush()
                    time.sleep(0.8)
            except (BrokenPipeError, ConnectionResetError, OSError):
                return

        self._send_json({"ok": False, "error": "Not found"}, status=404)

    def do_POST(self):
        path = urlparse(self.path).path
        eng = get_engine()
        body = self._read_json()

        if path == "/api/bot/start":
            ok, msg = eng.start()
            self._send_json({"ok": ok, "message": msg}, status=200 if ok else 400)
            return

        if path == "/api/bot/stop":
            ok, msg = eng.stop_bot()
            self._send_json({"ok": ok, "message": msg})
            return

        if path == "/api/bot/probe":
            res = bot.probe(bot.CONFIG, stream_mgr=eng.stream_mgr)
            self._send_json({"ok": True, "message": f"Probe complete: {res.get('candidates', 0)} candidates", "probe": res})
            return

        if path == "/api/mode":
            ok, msg = eng.set_mode(body.get("mode", "paper"))
            self._send_json({"ok": ok, "message": msg}, status=200 if ok else 400)
            return

        if path == "/api/phantom/connect":
            hint = str(body.get("wallet_hint") or "").strip()
            prof = str(body.get("chrome_profile") or "").strip() or None
            mode = str(body.get("connection_mode") or "").strip() or None
            ok, msg = eng.connect_phantom_once(wallet_hint=hint, chrome_profile=prof, connection_mode=mode)
            self._send_json({"ok": ok, "message": msg, "phantom": eng.bridge.get_status()}, status=200 if ok else 400)
            return

        if path == "/api/phantom/refresh":
            bal = eng.bridge.refresh_sol_balance()
            eng.live_acct.sync_wallet_cash()
            self._send_json({"ok": True, "message": f"Balance: {bal:.4f} SOL", "phantom": eng.bridge.get_status()})
            return

        if path == "/api/phantom/close":
            ok, msg = eng.close_phantom_browser()
            self._send_json({"ok": ok, "message": msg})
            return

        if path == "/api/stream/start":
            eng.start_stream_only()
            self._send_json({"ok": True, "message": "Real-time WS stream started."})
            return

        if path == "/api/stream/stop":
            eng.stop_stream()
            self._send_json({"ok": True, "message": "Real-time WS stream stopped."})
            return

        if path == "/api/stream/simulate":
            ev = eng.stream_mgr.feed_event(body)
            self._send_json({"ok": bool(ev), "event": ev})
            return

        if path == "/api/positions/sell":
            mint = str(body.get("mint") or "").strip()
            if not mint:
                self._send_json({"ok": False, "message": "Missing mint"}, status=400)
                return
            msg = eng.sell_now(mint)
            self._send_json({"ok": True, "message": msg})
            return

        if path == "/api/settings":
            parsed, errors = parse_settings(body)
            if errors:
                self._send_json({"ok": False, "errors": errors, "message": errors[0]}, status=400)
                return
            bot.CONFIG.update(parsed)
            save_settings()
            if parsed.get("trading_mode") != eng.mode and not eng.running:
                eng.set_mode(parsed["trading_mode"])
            self._send_json({"ok": True, "message": "Settings applied and saved."})
            return

        if path == "/api/account/reset":
            ok, msg = eng.reset_account()
            self._send_json({"ok": ok, "message": msg}, status=200 if ok else 400)
            return

        self._send_json({"ok": False, "error": "Not found"}, status=404)


def serve(host="0.0.0.0", port=3000):
    get_engine()
    server = ThreadingHTTPServer((host, port), DashboardHandler)
    print(f"Python Solana Sniper Web Dashboard listening on http://{host}:{port}", flush=True)

    def _shutdown(signum, frame):
        eng = get_engine()
        eng.shutdown()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _shutdown)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _shutdown(None, None)
    finally:
        server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Pure-Python Solana Sniper Web Dashboard")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 3000)))
    args = parser.parse_args()
    serve(host=args.host, port=args.port)
