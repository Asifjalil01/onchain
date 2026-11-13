#!/usr/bin/env python3
"""
Pure-Python Phantom Wallet Browser Integration & Live Swap Bridge.

Mirrors the full Phantom Live Wallet architecture in Python:
1. Browser Extension Detection (`window.phantom?.solana || window.solana` with `isPhantom`):
   - Silent auto-reconnect (`onlyIfTrusted: true`) + `connect`, `disconnect`, `accountChanged` listeners
   - Interactive `provider.connect()` popup trigger (or opens https://phantom.app/ if missing)
   - Never asks for or stores private keys or seed phrases
2. Live On-Chain Balance & Backend Sync:
   - Queries `getBalance` (`confirmed` commitment) across fallback Solana Mainnet RPCs
   - Tracks `gas_reserved_sol` (0.02 SOL), `signing_available`, `is_read_only`, `rpc_endpoint`
3. Live Swap Execution & On-Chain Parsed Receipt Verification:
   - Fetches Jupiter v6 quote + VersionedTransaction (`/v6/quote`, `/v6/swap`)
   - Dispatches serialized transaction to connected browser tab for Phantom popup approval
     (`signAndSendTransaction` or `signTransaction` + `sendRawTransaction`), handling code 4001 cleanly
   - Polls `getSignatureStatuses` (`confirmed`/`finalized`) and inspects `getParsedTransaction`
     for actual SOL delta, actual tokens received, fee lamports, and Solscan explorer URL
"""
import glob
import json
import os
import platform
import shutil
import socket
import subprocess
import threading
import time
import urllib.parse
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from http_client import HttpSession
from realtime_stream import PureWebSocket, WSOL_MINT

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None

PHANTOM_EXT_ID = "bfnaelmomeimhlpmgjnjophhpkkoljpa"
JUP_QUOTE_URLS = [
    "https://quote-api.jup.ag/v6",
    "https://api.jup.ag/swap/v1",
]
SOLANA_RPC_URLS = [
    "https://api.mainnet-beta.solana.com",
    "https://rpc.ankr.com/solana",
    "https://solana-rpc.publicnode.com",
]
LAMPORTS_PER_SOL = 1_000_000_000


def find_chromium_binary(custom_path=""):
    """Locate Chrome/Chromium binary across Windows, macOS, Linux, and Playwright cache."""
    candidates = []
    if custom_path:
        candidates.append(custom_path)
    for env_key in ("CHROMIUM_PATH", "CHROME_PATH"):
        if os.environ.get(env_key):
            candidates.append(os.environ[env_key])

    sys_name = platform.system().lower()
    if sys_name == "windows":
        local_app = os.environ.get("LOCALAPPDATA", "")
        prog_files = os.environ.get("PROGRAMFILES", r"C:\Program Files")
        prog_x86 = os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")
        candidates.extend([
            os.path.join(local_app, r"Google\Chrome\Application\chrome.exe"),
            os.path.join(prog_files, r"Google\Chrome\Application\chrome.exe"),
            os.path.join(prog_x86, r"Google\Chrome\Application\chrome.exe"),
            os.path.join(local_app, r"BraveSoftware\Brave-Browser\Application\brave.exe"),
            os.path.join(prog_x86, r"Microsoft\Edge\Application\msedge.exe"),
        ])
    elif sys_name == "darwin":
        candidates.extend([
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
        ])
    else:
        for cmd in ("google-chrome", "chromium", "chromium-browser", "brave-browser"):
            found = shutil.which(cmd)
            if found:
                candidates.append(found)
        candidates.extend([
            "/ms-playwright/chrome-headless-shell-linux64/chrome-headless-shell",
            "/usr/bin/google-chrome",
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
        ])
        pw_glob = glob.glob("/ms-playwright/chromium*/chrome-linux*/chrome")
        candidates.extend(pw_glob)

    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return None


def find_chrome_user_data_dir():
    """Return the OS default Google Chrome User Data directory if present."""
    sys_name = platform.system().lower()
    home = os.path.expanduser("~")
    if sys_name == "windows":
        la = os.environ.get("LOCALAPPDATA", "")
        candidates = [
            os.path.join(la, r"Google\Chrome\User Data"),
            os.path.join(la, r"BraveSoftware\Brave-Browser\User Data"),
        ]
    elif sys_name == "darwin":
        candidates = [
            os.path.join(home, "Library/Application Support/Google/Chrome"),
            os.path.join(home, "Library/Application Support/BraveSoftware/Brave-Browser"),
        ]
    else:
        candidates = [
            os.path.join(home, ".config/google-chrome"),
            os.path.join(home, ".config/chromium"),
            os.path.join(home, ".config/BraveSoftware/Brave-Browser"),
        ]
    for p in candidates:
        if p and os.path.isdir(p):
            return p
    return ""


def list_chrome_profiles(user_data_dir=""):
    """Read Chrome `Local State` to list available existing profiles (`Default`, `Profile 1`, ...)."""
    ud = user_data_dir or find_chrome_user_data_dir()
    if not ud:
        return [{"dir": "Default", "name": "Default"}]
    local_state = os.path.join(ud, "Local State")
    profiles = []
    if os.path.isfile(local_state):
        try:
            with open(local_state, encoding="utf-8") as f:
                data = json.load(f)
            info_cache = ((data.get("profile") or {}).get("info_cache")) or {}
            for p_dir, meta in info_cache.items():
                if isinstance(meta, dict):
                    profiles.append({"dir": p_dir, "name": meta.get("name") or p_dir})
        except Exception:
            pass
    if not profiles:
        for d in ("Default", "Profile 1", "Profile 2"):
            if os.path.isdir(os.path.join(ud, d)):
                profiles.append({"dir": d, "name": d})
    return profiles or [{"dir": "Default", "name": "Default"}]


def find_phantom_extension(custom_path="", chrome_profile="Default"):
    """Locate an unpacked Phantom browser extension directory in the user's Chrome profile."""
    if custom_path and os.path.isdir(custom_path):
        if os.path.isfile(os.path.join(custom_path, "manifest.json")):
            return custom_path
        subs = sorted(glob.glob(os.path.join(custom_path, "*")), reverse=True)
        for s in subs:
            if os.path.isfile(os.path.join(s, "manifest.json")):
                return s

    ud = find_chrome_user_data_dir()
    search_roots = []
    if ud:
        search_roots.append(os.path.join(ud, chrome_profile or "Default", "Extensions", PHANTOM_EXT_ID))
        search_roots.append(os.path.join(ud, "Default", "Extensions", PHANTOM_EXT_ID))

    for root in search_roots:
        if os.path.isdir(root):
            versions = sorted(glob.glob(os.path.join(root, "*")), reverse=True)
            for v in versions:
                if os.path.isfile(os.path.join(v, "manifest.json")):
                    return v
    return ""


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _CDPPageClient:
    """Minimal pure-Python Chrome DevTools Protocol client over PureWebSocket."""

    def __init__(self, ws_url):
        self.ws_url = ws_url
        self.ws = PureWebSocket(ws_url, timeout=15.0).connect()
        self._id = 0
        self._lock = threading.Lock()

    def call(self, method, params=None, timeout=12.0):
        with self._lock:
            self._id += 1
            req_id = self._id
            self.ws.send_json({"id": req_id, "method": method, "params": params or {}})
            deadline = time.time() + timeout
            while time.time() < deadline:
                raw = self.ws.recv()
                if raw is None:
                    raise RuntimeError("CDP WebSocket closed")
                msg = json.loads(raw)
                if msg.get("id") == req_id:
                    if "error" in msg:
                        raise RuntimeError(msg["error"].get("message", "CDP error"))
                    return msg.get("result", {})
            raise TimeoutError(f"CDP call timed out: {method}")

    def eval_js(self, expression, await_promise=True):
        res = self.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "awaitPromise": await_promise,
                "returnByValue": True,
            },
        )
        exc = res.get("exceptionDetails")
        if exc:
            txt = exc.get("text") or (exc.get("exception") or {}).get("description") or "JS error"
            raise RuntimeError(txt)
        return (res.get("result") or {}).get("value")

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


_RELAY_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>Solana Sniper - Phantom Wallet Bridge</title>
  <style>
    body {
      margin: 0; font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
      background: #0b0f17; color: #e6edf7; display: flex; align-items: center;
      justify-content: center; min-height: 100vh; padding: 20px;
    }
    .card {
      background: #121824; border: 1px solid #243047; border-radius: 14px;
      padding: 24px 28px; max-width: 580px; width: 100%;
    }
    h1 { margin: 0 0 8px; font-size: 20px; }
    p { color: #94a3b8; font-size: 13.5px; line-height: 1.5; }
    .status {
      margin: 14px 0; padding: 12px 14px; border-radius: 8px;
      background: #182030; border: 1px solid #28354d; font-family: monospace; font-size: 13px;
    }
    .ok { border-color: #10b981; color: #6ee7b7; }
    .err { border-color: #ef4444; color: #fca5a5; }
    .row { display: flex; gap: 10px; flex-wrap: wrap; }
    button {
      background: #6d28d9; color: #fff; border: none; border-radius: 8px;
      padding: 10px 18px; font-size: 14px; font-weight: 600; cursor: pointer;
    }
    button:hover { background: #5b21b6; }
    button.sec { background: #1e293b; border: 1px solid #334155; }
    .log {
      margin-top: 14px; background: #080b11; border: 1px solid #1f2937;
      border-radius: 8px; padding: 10px; height: 160px; overflow-y: auto;
      font-family: monospace; font-size: 12px; color: #94a3b8;
    }
  </style>
  <script src="https://unpkg.com/@solana/web3.js@1.95.8/lib/index.iife.min.js" defer></script>
</head>
<body>
  <div class="card">
    <h1>Solana Sniper &mdash; Phantom Browser Bridge</h1>
    <p>Uses your existing Chrome profile's Phantom browser extension (no private keys stored). Keep this tab open to approve and sign live swaps directly in Phantom.</p>
    <div id="status" class="status">Detecting window.phantom?.solana...</div>
    <div class="row">
      <button id="btn-connect" type="button" onclick="connectPhantomBrowser()">Connect Phantom Wallet</button>
      <button id="btn-disconnect" type="button" class="sec" onclick="disconnectPhantom()">Disconnect</button>
    </div>
    <div id="log" class="log"></div>
  </div>
  <script>
    const statusEl = document.getElementById('status');
    const logEl = document.getElementById('log');
    const RPC_LIST = [
      'https://api.mainnet-beta.solana.com',
      'https://rpc.ankr.com/solana',
      'https://solana-rpc.publicnode.com'
    ];
    let connectedPubkey = null;
    let busyJob = false;

    function addLog(msg) {
      const line = document.createElement('div');
      line.textContent = `[${new Date().toLocaleTimeString()}] ${msg}`;
      logEl.appendChild(line);
      logEl.scrollTop = logEl.scrollHeight;
    }

    function getProvider() {
      const p = (window.phantom && window.phantom.solana) || window.solana;
      return (p && p.isPhantom) ? p : null;
    }

    function bindListeners() {
      const p = getProvider();
      if (!p || !p.on || p.__sniperBound) return;
      p.__sniperBound = true;
      try {
        p.on('connect', async (pk) => {
          const addr = pk && pk.toString ? pk.toString() : null;
          if (addr) await onWalletConnected(addr);
        });
        p.on('disconnect', async () => {
          await onWalletDisconnected();
        });
        p.on('accountChanged', async (pk) => {
          if (pk && pk.toString) await onWalletConnected(pk.toString());
          else await onWalletDisconnected();
        });
      } catch {}
    }

    async function onWalletConnected(addr) {
      connectedPubkey = addr;
      statusEl.className = 'status ok';
      statusEl.textContent = `Connected: ${addr}`;
      await fetch('/api/wallets/connect', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          chain: 'solana',
          wallet_id: 'phantom',
          name: 'Phantom Wallet',
          type: 'solana',
          address: addr,
          signing_available: true,
          is_read_only: false,
          network: 'Solana Mainnet-Beta'
        })
      });
      addLog(`Synced Phantom address ${addr} to Python backend`);
    }

    async function onWalletDisconnected() {
      connectedPubkey = null;
      statusEl.className = 'status';
      statusEl.textContent = 'Phantom disconnected.';
      await fetch('/api/wallets/disconnect', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ chain: 'solana' })
      }).catch(() => {});
      addLog('Phantom disconnected');
    }

    async function attemptAutoReconnect() {
      const p = getProvider();
      if (!p) return false;
      try {
        if (p.isConnected && p.publicKey) {
          await onWalletConnected(p.publicKey.toString());
          return true;
        }
        const resp = await p.connect({ onlyIfTrusted: true });
        if (resp && resp.publicKey) {
          await onWalletConnected(resp.publicKey.toString());
          return true;
        }
      } catch {}
      return false;
    }

    async function connectPhantomBrowser() {
      const p = getProvider();
      if (!p) {
        statusEl.className = 'status err';
        statusEl.textContent = 'Phantom extension not found in this Chrome profile. Opening https://phantom.app/...';
        addLog('Phantom extension missing — opening phantom.app');
        window.open('https://phantom.app/', '_blank', 'noopener,noreferrer');
        return false;
      }
      try {
        const resp = await p.connect();
        const addr = (resp && resp.publicKey && resp.publicKey.toString()) || (p.publicKey && p.publicKey.toString());
        if (addr) {
          await onWalletConnected(addr);
          return true;
        }
      } catch (err) {
        statusEl.className = 'status err';
        statusEl.textContent = (err && err.code === 4001)
          ? 'Connection declined in Phantom popup.'
          : 'Connect error: ' + ((err && err.message) || err);
        addLog(statusEl.textContent);
      }
      return false;
    }

    async function disconnectPhantom() {
      const p = getProvider();
      try { if (p && p.disconnect) await p.disconnect(); } catch {}
      await onWalletDisconnected();
    }

    function b64ToBytes(b64) {
      const bin = atob(b64);
      const out = new Uint8Array(bin.length);
      for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
      return out;
    }

    async function sendRawViaRpc(rawBytes) {
      let b64 = '';
      for (let i = 0; i < rawBytes.length; i++) b64 += String.fromCharCode(rawBytes[i]);
      const encoded = btoa(b64);
      for (const rpc of RPC_LIST) {
        try {
          const r = await fetch(rpc, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              jsonrpc: '2.0', id: 1, method: 'sendTransaction',
              params: [encoded, { encoding: 'base64', skipPreflight: false, preflightCommitment: 'confirmed', maxRetries: 3 }]
            })
          });
          const j = await r.json();
          if (j && j.result) return j.result;
        } catch {}
      }
      throw new Error('Failed to broadcast signed transaction');
    }

    async function handleSignJob(job) {
      const p = getProvider();
      if (!p) throw new Error('Phantom provider missing');
      if (!connectedPubkey) await connectPhantomBrowser();
      const bytes = b64ToBytes(job.tx_b64);
      let txObj;
      if (window.solanaWeb3 && window.solanaWeb3.VersionedTransaction) {
        txObj = window.solanaWeb3.VersionedTransaction.deserialize(bytes);
      } else {
        txObj = { serialize: () => bytes };
      }
      addLog(`Opening Phantom popup to approve ${job.side || 'SWAP'}...`);
      if (typeof p.signAndSendTransaction === 'function') {
        const res = await p.signAndSendTransaction(txObj, { skipPreflight: false, maxRetries: 3 });
        return res.signature || String(res);
      }
      const signedTx = await p.signTransaction(txObj);
      return await sendRawViaRpc(signedTx.serialize());
    }

    async function pollJobs() {
      if (!busyJob) {
        try {
          const res = await fetch('/api/relay/next');
          if (res.ok) {
            const job = await res.json();
            if (job && job.id && job.tx_b64) {
              busyJob = true;
              try {
                const sig = await handleSignJob(job);
                addLog(`Approved & broadcast! sig=${sig}`);
                await fetch('/api/relay/result', {
                  method: 'POST',
                  headers: { 'Content-Type': 'application/json' },
                  body: JSON.stringify({ id: job.id, ok: true, signature: sig })
                });
              } catch (err) {
                const isReject = (err && err.code === 4001) || String((err && err.message) || err).toLowerCase().includes('reject');
                const msg = isReject ? 'User declined transaction in Phantom (code 4001).' : String((err && err.message) || err);
                addLog(msg);
                await fetch('/api/relay/result', {
                  method: 'POST',
                  headers: { 'Content-Type': 'application/json' },
                  body: JSON.stringify({ id: job.id, ok: false, rejected: isReject, error: msg })
                });
              } finally {
                busyJob = false;
              }
            }
          }
        } catch {}
      }
      setTimeout(pollJobs, 300);
    }

    window.addEventListener('load', async () => {
      setTimeout(async () => {
        bindListeners();
        const reconnected = await attemptAutoReconnect();
        if (!reconnected) {
          const p = getProvider();
          statusEl.textContent = p
            ? 'Phantom detected — click "Connect Phantom Wallet" to approve.'
            : 'Phantom extension not detected in this browser.';
        }
        pollJobs();
      }, 250);
    });
  </script>
</body>
</html>
"""


class _LocalPhantomRelay:
    """
    Lightweight pure-Python HTTP server used when connecting from Desktop GUI (`gui.py`).
    Bridges the user's existing Chrome tab directly with `PhantomBrowserBridge`.
    """

    def __init__(self, bridge):
        self.bridge = bridge
        self._server = None
        self.port = None

    def start(self):
        if self._server is not None:
            return self.port
        bridge = self.bridge

        class RelayHandler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                pass

            def _json(self, obj, code=200):
                b = json.dumps(obj).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(b)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(b)

            def _read_json(self):
                n = int(self.headers.get("Content-Length") or 0)
                if n <= 0:
                    return {}
                try:
                    return json.loads(self.rfile.read(n).decode("utf-8"))
                except Exception:
                    return {}

            def do_GET(self):
                p = urllib.parse.urlparse(self.path).path
                if p in ("/", "/phantom-bridge"):
                    body = _RELAY_HTML.encode("utf-8")
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if p == "/api/relay/next":
                    job = bridge.pop_pending_sign_job()
                    self._json(job or {})
                    return
                self._json({}, code=404)

            def do_POST(self):
                p = urllib.parse.urlparse(self.path).path
                data = self._read_json()
                if p in ("/api/relay/connected", "/api/wallets/connect"):
                    pub = str(data.get("address") or data.get("publicKey") or "").strip()
                    if pub:
                        bridge.set_connected_wallet(
                            address=pub,
                            signing_available=bool(data.get("signing_available", True)),
                            is_read_only=bool(data.get("is_read_only", False)),
                            driver="existing_chrome_relay",
                        )
                    self._json({"ok": bool(pub), "phantom": bridge.get_status()})
                    return
                if p == "/api/wallets/disconnect":
                    bridge.disconnect_wallet()
                    self._json({"ok": True})
                    return
                if p == "/api/relay/result":
                    jid = str(data.get("id") or "")
                    bridge.submit_sign_result(jid, data)
                    self._json({"ok": True})
                    return
                self._json({}, code=404)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), RelayHandler)
        self.port = self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self.port

    def url(self):
        port = self.start()
        return f"http://127.0.0.1:{port}/phantom-bridge"

    def stop(self):
        if self._server is not None:
            try:
                self._server.shutdown()
                self._server.server_close()
            except Exception:
                pass
            self._server = None
            self.port = None


class PhantomBrowserBridge:
    """
    Browser-Once Phantom automation & signing bridge.
    Works seamlessly with:
    - Web Dashboard (`http://localhost:3000`) directly via `window.phantom.solana`
    - Existing Chrome Profile tab (`_LocalPhantomRelay`) for Desktop Tkinter GUI
    - Existing Chrome CDP attach (`--remote-debugging-port=9222`) for auto-confirm
    """

    def __init__(
        self,
        profile_dir="phantom_profile",
        extension_path="",
        auto_confirm=True,
        headless=False,
        rpc_urls=None,
        jupiter_api_key="",
        connection_mode="existing_chrome",
        chrome_profile="Default",
        cdp_port=9222,
        gas_reserved_sol=0.02,
        log_fn=None,
    ):
        self.profile_dir = os.path.abspath(profile_dir)
        self.extension_path = extension_path
        self.auto_confirm = auto_confirm
        self.headless = headless
        self.rpc_urls = list(rpc_urls or SOLANA_RPC_URLS)
        self.jupiter_api_key = jupiter_api_key
        self.connection_mode = connection_mode or "existing_chrome"
        self.chrome_profile = chrome_profile or "Default"
        self.cdp_port = int(cdp_port or 9222)
        self.gas_reserved_sol = float(gas_reserved_sol)
        self._log = log_fn or (lambda m: None)

        self._http = HttpSession()
        self._lock = threading.RLock()
        self._session_file = os.path.join(self.profile_dir, "phantom_session.json")

        # Shared signing relay queue (consumed by either Web Dashboard tab OR _LocalPhantomRelay tab)
        self._relay_lock = threading.Lock()
        self._pending_job = None
        self._job_results = {}
        self.last_tab_heartbeat = 0.0

        # Runtime handles
        self._pw = None
        self._pw_ctx = None
        self._cdp_proc = None
        self._cdp_port = None
        self._driver = None
        self._relay = _LocalPhantomRelay(self)

        # Wallet & RPC state
        self.status = "disconnected"  # disconnected | connecting | awaiting_unlock | connected | paired_idle | error
        self.wallet_address = ""
        self.sol_balance = 0.0
        self.balance_usd = 0.0
        self.sol_price_usd = 180.0
        self.rpc_endpoint = self.rpc_urls[0]
        self.signing_available = False
        self.is_read_only = False
        self.connected_once = False
        self.last_error = ""
        self.last_tx_sig = ""
        self.last_checked_at = 0.0
        self.browser_pid = None

        self._load_session()

    # ---------- Shared relay queue methods (used by web_dashboard.py & _LocalPhantomRelay)
    def mark_tab_heartbeat(self):
        self.last_tab_heartbeat = time.time()

    def is_tab_alive(self, max_age=8.0):
        return self.last_tab_heartbeat > 0 and (time.time() - self.last_tab_heartbeat) <= max_age

    def pop_pending_sign_job(self):
        self.mark_tab_heartbeat()
        with self._relay_lock:
            job = self._pending_job
            self._pending_job = None
            return job

    def submit_sign_result(self, job_id, result_dict):
        if not job_id:
            return
        with self._relay_lock:
            self._job_results[job_id] = dict(result_dict or {})

    def submit_sign_job_to_browser(self, tx_b64, side="BUY", timeout_sec=45.0):
        job_id = f"job_{int(time.time() * 1000)}"
        with self._relay_lock:
            self._pending_job = {"id": job_id, "tx_b64": tx_b64, "side": side}
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            with self._relay_lock:
                res = self._job_results.pop(job_id, None)
            if res is not None:
                if res.get("ok") and res.get("signature"):
                    return res["signature"], None
                return None, res.get("error") or "User rejected or Phantom signing failed"
            time.sleep(0.25)
        return None, "Timed out waiting for Phantom browser popup approval"

    # ---------- Wallet sync & balance
    def set_connected_wallet(self, address, signing_available=True, is_read_only=False, driver=None, sol_price_usd=None):
        addr = str(address or "").strip()
        if not addr:
            return
        with self._lock:
            changed = self.wallet_address != addr or not self.connected_once
            self.wallet_address = addr
            self.connected_once = True
            self.status = "connected"
            self.signing_available = bool(signing_available)
            self.is_read_only = bool(is_read_only)
            self.last_error = ""
            if sol_price_usd and sol_price_usd > 1.0:
                self.sol_price_usd = float(sol_price_usd)
            if driver:
                self._driver = driver
            elif not self._driver:
                self._driver = "phantom_browser_extension"
            self.mark_tab_heartbeat()
            self._save_session()
        self.refresh_sol_balance(sol_price_usd=self.sol_price_usd)
        if changed:
            self._log(f"Phantom wallet connected ({self._driver}): {addr} | {self.sol_balance:.5f} SOL (${self.balance_usd:.2f})")

    def disconnect_wallet(self):
        with self._lock:
            self.wallet_address = ""
            self.connected_once = False
            self.status = "disconnected"
            self.sol_balance = 0.0
            self.balance_usd = 0.0
            self.signing_available = False
            self.is_read_only = False
            self.last_error = ""
            self._save_session()
        self._log("Phantom wallet disconnected.")

    def _load_session(self):
        if os.path.isfile(self._session_file):
            try:
                with open(self._session_file, encoding="utf-8") as f:
                    data = json.load(f)
                self.wallet_address = data.get("wallet_address", "")
                self.connected_once = bool(data.get("connected_once", False))
                self.last_tx_sig = data.get("last_tx_sig", "")
                if self.connected_once and self.wallet_address:
                    self.status = "paired_idle"
            except Exception:
                pass

    def _save_session(self):
        os.makedirs(self.profile_dir, exist_ok=True)
        payload = {
            "wallet_address": self.wallet_address,
            "connected_once": self.connected_once,
            "last_tx_sig": self.last_tx_sig,
            "profile_dir": self.profile_dir,
            "chrome_profile": self.chrome_profile,
            "connection_mode": self.connection_mode,
            "updated_at": time.time(),
        }
        tmp = self._session_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp, self._session_file)

    def get_status(self):
        with self._lock:
            ext_found = find_phantom_extension(self.extension_path, self.chrome_profile)
            bin_found = find_chromium_binary()
            return {
                "status": self.status,
                "connected_once": self.connected_once,
                "wallet_address": self.wallet_address,
                "sol_balance": round(self.sol_balance, 6),
                "balance_usd": round(self.balance_usd, 2),
                "gas_reserved_sol": self.gas_reserved_sol,
                "signing_available": self.signing_available,
                "is_read_only": self.is_read_only,
                "rpc_endpoint": self.rpc_endpoint,
                "auto_confirm": self.auto_confirm,
                "connection_mode": self.connection_mode,
                "chrome_profile": self.chrome_profile,
                "cdp_port": self.cdp_port,
                "profile_dir": self.profile_dir,
                "relay_url": f"http://127.0.0.1:{self._relay.port}/phantom-bridge" if self._relay.port else "",
                "relay_tab_alive": self.is_tab_alive(),
                "extension_detected": bool(ext_found),
                "extension_path": ext_found or self.extension_path,
                "chromium_detected": bool(bin_found),
                "chromium_path": bin_found or "",
                "driver": self._driver or self.connection_mode,
                "browser_active": self.is_browser_running(),
                "last_tx_sig": self.last_tx_sig,
                "last_error": self.last_error,
            }

    def is_browser_running(self):
        if self.is_tab_alive():
            return True
        if self._pw_ctx is not None:
            return True
        if self._cdp_proc is not None and self._cdp_proc.poll() is None:
            return True
        if self._cdp_port is not None:
            return bool(self._cdp_targets())
        return False

    def refresh_sol_balance(self, wallet=None, sol_price_usd=None):
        addr = wallet or self.wallet_address
        if sol_price_usd and sol_price_usd > 1.0:
            self.sol_price_usd = float(sol_price_usd)
        if not addr:
            return 0.0
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getBalance",
            "params": [addr, {"commitment": "confirmed"}],
        }
        for rpc in self.rpc_urls:
            try:
                r = self._http.post(rpc, json=body, timeout=5)
                if r.status_code != 200:
                    continue
                val = (r.json().get("result") or {}).get("value")
                if val is not None:
                    self.sol_balance = float(val) / LAMPORTS_PER_SOL
                    self.balance_usd = round(self.sol_balance * self.sol_price_usd, 2)
                    self.rpc_endpoint = rpc
                    self.last_checked_at = time.time()
                    return self.sol_balance
            except Exception:
                continue
        return self.sol_balance

    # ---------- Browser launch / existing Chrome connection
    def _probe_existing_cdp(self):
        ports = []
        for p in (self.cdp_port, 9222, 9229):
            if p and p not in ports:
                ports.append(p)
        for port in ports:
            try:
                r = self._http.get(f"http://127.0.0.1:{port}/json/list", timeout=1.0)
                if r.status_code == 200 and isinstance(r.json(), list):
                    return port
            except Exception:
                continue
        return None

    def _open_url_in_existing_chrome(self, url):
        chrome_bin = find_chromium_binary()
        if chrome_bin:
            cmd = [chrome_bin]
            if self.chrome_profile:
                cmd.append(f"--profile-directory={self.chrome_profile}")
            cmd.append(url)
            try:
                subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return True, f"Opened bridge tab in existing Chrome profile '{self.chrome_profile}'"
            except Exception as e:
                self._log(f"Direct Chrome tab launch fallback: {e}")

        webbrowser.open(url, new=2)
        return True, "Opened bridge tab in default browser"

    def launch_persistent_browser(self, open_url="http://127.0.0.1", headless=None):
        with self._lock:
            if self.is_browser_running():
                return True, f"Browser already running ({self._driver})"

            os.makedirs(self.profile_dir, exist_ok=True)
            ext_dir = find_phantom_extension(self.extension_path, self.chrome_profile)
            use_headless = self.headless if headless is None else headless
            args = [
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-blink-features=AutomationControlled",
            ]
            if ext_dir:
                args.extend([
                    f"--disable-extensions-except={ext_dir}",
                    f"--load-extension={ext_dir}",
                ])
                use_headless = False

            self.status = "launching"
            self.last_error = ""

            chrome_bin = find_chromium_binary()
            if sync_playwright is not None and chrome_bin and not use_headless and platform.system().lower() in ("windows", "darwin"):
                try:
                    self._pw = sync_playwright().start()
                    self._pw_ctx = self._pw.chromium.launch_persistent_context(
                        self.profile_dir,
                        channel="chrome",
                        headless=use_headless,
                        args=args,
                    )
                    self._driver = "playwright"
                    page = self._pw_ctx.pages[0] if self._pw_ctx.pages else self._pw_ctx.new_page()
                    page.goto(open_url, wait_until="domcontentloaded", timeout=20000)
                    return True, "Launched isolated Chrome profile"
                except Exception:
                    self._cleanup_pw()

            if not chrome_bin:
                self.status = "error"
                self.last_error = "No Chrome/Chromium binary found. Install Chrome or set CHROMIUM_PATH."
                return False, self.last_error

            port = _free_port()
            cmd = [
                chrome_bin,
                f"--user-data-dir={self.profile_dir}",
                f"--remote-debugging-port={port}",
                "--remote-allow-origins=*",
                "--no-sandbox",
                "--disable-gpu",
                *args,
            ]
            if use_headless:
                cmd.append("--headless")
            cmd.append(open_url)

            try:
                self._cdp_proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self._cdp_port = port
                self.browser_pid = self._cdp_proc.pid
                deadline = time.time() + 10.0
                ready = False
                while time.time() < deadline:
                    if self._cdp_proc.poll() is not None:
                        break
                    try:
                        r = self._http.get(f"http://127.0.0.1:{port}/json/list", timeout=1.5)
                        if r.status_code == 200:
                            ready = True
                            break
                    except Exception:
                        pass
                    time.sleep(0.25)

                if not ready:
                    self.close()
                    self.status = "error"
                    self.last_error = "Chrome launched but CDP port did not respond."
                    return False, self.last_error

                self._driver = "cdp"
                return True, f"Launched CDP browser (port {port})"
            except Exception as e:
                self.status = "error"
                self.last_error = str(e)
                return False, self.last_error

    def _cdp_targets(self):
        if not self._cdp_port:
            return []
        try:
            r = self._http.get(f"http://127.0.0.1:{self._cdp_port}/json/list", timeout=2.0)
            data = r.json()
            return data if isinstance(data, list) else []
        except Exception:
            return []

    def _auto_click_phantom_popup_cdp(self, keywords=("Connect", "Approve", "Confirm"), timeout=15.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            for t in self._cdp_targets():
                url = str(t.get("url", ""))
                if PHANTOM_EXT_ID in url or "notification.html" in url or "popup.html" in url:
                    ws_url = t.get("webSocketDebuggerUrl")
                    if not ws_url:
                        continue
                    try:
                        client = _CDPPageClient(ws_url)
                        try:
                            js = f"""
                            (() => {{
                                const btns = Array.from(document.querySelectorAll('button'));
                                const wants = {json.dumps([k.lower() for k in keywords])};
                                for (const b of btns) {{
                                    const txt = (b.innerText || b.textContent || '').trim().toLowerCase();
                                    if (!b.disabled && wants.some(w => txt.includes(w))) {{
                                        b.click();
                                        return 'clicked:' + txt;
                                    }}
                                }}
                                return 'waiting';
                            }})()
                            """
                            res = client.eval_js(js)
                            if isinstance(res, str) and res.startswith("clicked:"):
                                return True, res
                        finally:
                            client.close()
                    except Exception:
                        pass
            time.sleep(0.35)
        return False, "No Phantom popup clicked before timeout"

    def _auto_click_phantom_popup_pw(self, keywords=("Connect", "Approve", "Confirm"), timeout=15.0):
        if not self._pw_ctx:
            return False, "No Playwright context"
        deadline = time.time() + timeout
        while time.time() < deadline:
            for p in list(self._pw_ctx.pages):
                if PHANTOM_EXT_ID in p.url or "notification.html" in p.url:
                    for kw in keywords:
                        try:
                            btn = p.locator(f"button:has-text('{kw}')").first
                            if btn.is_visible(timeout=400) and btn.is_enabled():
                                btn.click()
                                return True, f"clicked:{kw}"
                        except Exception:
                            continue
            time.sleep(0.3)
        return False, "No Phantom popup clicked before timeout"

    def connect_once(self, wait_unlock_sec=45, wallet_hint="", dashboard_url=None):
        """
        Browser-Once connection handshake.
        - If `wallet_hint` is supplied, pairs read-only/RPC address directly.
        - Otherwise opens the Phantom Bridge tab in the user's already-open Chrome
          (or attaches via CDP if exposed on `cdp_port`).
        """
        self.last_error = ""
        if wallet_hint and len(wallet_hint.strip()) >= 32:
            self.set_connected_wallet(
                address=wallet_hint.strip(),
                signing_available=False,
                is_read_only=True,
                driver="manual_address_readonly",
            )
            return True, f"Connected read-only address ({self.wallet_address[:6]}...)"

        target_url = dashboard_url or self._relay.url()

        if self.connection_mode in ("existing_chrome", "cdp_attach"):
            cdp_port = self._probe_existing_cdp()
            if cdp_port is not None:
                self._cdp_port = cdp_port
                self._driver = "cdp_attached"
                try:
                    encoded = urllib.parse.quote(target_url, safe="")
                    self._http.get(f"http://127.0.0.1:{cdp_port}/json/new?{encoded}", timeout=3.0)
                except Exception:
                    pass
                self._log(f"Attached to already-open Chrome CDP on port {cdp_port}")
                if self.auto_confirm:
                    threading.Thread(
                        target=self._auto_click_phantom_popup_cdp,
                        args=(("Connect", "Approve"), 15.0),
                        daemon=True,
                    ).start()
                deadline = time.time() + min(wait_unlock_sec, 18)
                while time.time() < deadline:
                    if self.connected_once and self.wallet_address and self.is_tab_alive():
                        return True, f"Connected via existing Chrome CDP: {self.wallet_address}"
                    time.sleep(0.35)
                self.status = "awaiting_unlock"
                return False, "Opened bridge tab in existing Chrome (CDP) — approve 'Connect' in Phantom once."

            if self.connection_mode == "cdp_attach":
                self.status = "error"
                self.last_error = f"No Chrome CDP found on port {self.cdp_port}."
                return False, self.last_error

            self.status = "awaiting_unlock"
            self._driver = "existing_chrome_relay"
            self._open_url_in_existing_chrome(target_url)
            self._log(f"Opened Phantom Bridge tab in your existing Chrome ({self.chrome_profile}): {target_url}")
            deadline = time.time() + min(wait_unlock_sec, 20)
            while time.time() < deadline:
                if self.is_tab_alive() and self.connected_once and self.wallet_address:
                    return True, f"Phantom connected in existing Chrome: {self.wallet_address}"
                time.sleep(0.4)
            return False, (
                f"Bridge tab opened in Chrome ({self.chrome_profile}). "
                "Approve 'Connect' in Phantom once; keep that bridge tab open for Live Mode."
            )

        ok, msg = self.launch_persistent_browser(open_url=target_url)
        if not ok:
            return False, msg
        self.status = "awaiting_unlock"
        if self._driver == "cdp" and self.auto_confirm:
            threading.Thread(
                target=self._auto_click_phantom_popup_cdp,
                args=(("Connect", "Approve"), 15.0),
                daemon=True,
            ).start()
        elif self._driver == "playwright" and self.auto_confirm:
            threading.Thread(
                target=self._auto_click_phantom_popup_pw,
                args=(("Connect", "Approve"), 15.0),
                daemon=True,
            ).start()
        deadline = time.time() + min(wait_unlock_sec, 20)
        while time.time() < deadline:
            if self.is_tab_alive() and self.connected_once and self.wallet_address:
                return True, f"Phantom connected: {self.wallet_address}"
            time.sleep(0.4)
        return False, "Isolated browser opened — unlock Phantom once and approve Connect."

    # ---------- Jupiter v6 Swap + On-Chain Parsed Receipt Verification
    def fetch_jupiter_swap_tx(self, input_mint, output_mint, amount_raw, slippage_bps=150, priority_fee_lamports=50000, user_pubkey=None):
        """Fetch live Jupiter v6 quote + serialized VersionedTransaction (base64)."""
        pubkey = user_pubkey or self.wallet_address
        if not pubkey:
            return None, "No Phantom wallet address connected."
        hdrs = {"Accept": "application/json"}
        if self.jupiter_api_key:
            hdrs["x-api-key"] = self.jupiter_api_key

        last_err = "Jupiter quote failed"
        for base in JUP_QUOTE_URLS:
            try:
                q_resp = self._http.get(
                    f"{base}/quote",
                    params={
                        "inputMint": input_mint,
                        "outputMint": output_mint,
                        "amount": str(int(amount_raw)),
                        "slippageBps": str(int(slippage_bps)),
                        "swapMode": "ExactIn",
                    },
                    headers=hdrs,
                    timeout=8,
                )
                q_resp.raise_for_status()
                quote = q_resp.json()
                if not isinstance(quote, dict) or not quote.get("outAmount"):
                    last_err = f"Invalid quote from {base}"
                    continue

                s_resp = self._http.post(
                    f"{base}/swap",
                    json={
                        "quoteResponse": quote,
                        "userPublicKey": pubkey,
                        "wrapAndUnwrapSol": True,
                        "dynamicComputeUnitLimit": True,
                        "prioritizationFeeLamports": priority_fee_lamports,
                    },
                    headers=hdrs,
                    timeout=10,
                )
                s_resp.raise_for_status()
                swap_data = s_resp.json()
                tx_b64 = swap_data.get("swapTransaction")
                if tx_b64:
                    return {"quote": quote, "swapTransaction": tx_b64, "api": base}, None
            except Exception as e:
                last_err = str(e)
                continue
        return None, last_err

    def confirm_and_verify_transaction(self, sig, target_mint=None, user_pubkey=None, timeout_sec=35.0):
        """
        Poll `getSignatureStatuses` until confirmed/finalized, then inspect
        `getParsedTransaction` to verify actual SOL spent/received, tokens received,
        and fee lamports (matching phantomLiveWallet.ts).
        """
        deadline = time.time() + timeout_sec
        confirmed = False
        status_err = None
        used_rpc = self.rpc_endpoint or self.rpc_urls[0]

        status_body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getSignatureStatuses",
            "params": [[sig], {"searchTransactionHistory": True}],
        }
        while time.time() < deadline:
            for rpc in self.rpc_urls:
                try:
                    r = self._http.post(rpc, json=status_body, timeout=5)
                    if r.status_code != 200:
                        continue
                    val = ((r.json().get("result") or {}).get("value") or [None])[0]
                    if isinstance(val, dict):
                        if val.get("err"):
                            return {
                                "confirmed": False,
                                "explorer_url": f"https://solscan.io/tx/{sig}",
                                "error": f"On-chain error: {json.dumps(val['err'])}",
                            }
                        st = val.get("confirmationStatus")
                        if st in ("confirmed", "finalized"):
                            confirmed = True
                            used_rpc = rpc
                            break
                except Exception:
                    continue
            if confirmed:
                break
            time.sleep(1.2)

        if not confirmed:
            return {
                "confirmed": False,
                "explorer_url": f"https://solscan.io/tx/{sig}",
                "error": status_err or "Transaction confirmation timed out after 35s.",
            }

        actual_tokens = None
        actual_sol_spent = None
        actual_sol_received = None
        fee_lamports = None
        pubkey = user_pubkey or self.wallet_address

        parsed_body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getParsedTransaction",
            "params": [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0, "commitment": "confirmed"}],
        }
        try:
            r = self._http.post(used_rpc, json=parsed_body, timeout=8)
            if r.status_code == 200:
                tx = (r.json().get("result") or {})
                meta = tx.get("meta") or {}
                fee_lamports = meta.get("fee")
                if pubkey and meta:
                    keys = ((tx.get("transaction") or {}).get("message") or {}).get("accountKeys") or []
                    key_strs = [k.get("pubkey") if isinstance(k, dict) else str(k) for k in keys]
                    if pubkey in key_strs:
                        idx = key_strs.index(pubkey)
                        pre_b = (meta.get("preBalances") or [])[idx]
                        post_b = (meta.get("postBalances") or [])[idx]
                        diff = pre_b - post_b
                        if diff > 0:
                            actual_sol_spent = max(0.0, (diff - (fee_lamports or 0)) / LAMPORTS_PER_SOL)
                        elif diff < 0:
                            actual_sol_received = abs(diff) / LAMPORTS_PER_SOL
                    if target_mint:
                        pre_tok = sum(
                            float((b.get("uiTokenAmount") or {}).get("uiAmount") or 0)
                            for b in (meta.get("preTokenBalances") or [])
                            if b.get("owner") == pubkey and b.get("mint") == target_mint
                        )
                        post_tok = sum(
                            float((b.get("uiTokenAmount") or {}).get("uiAmount") or 0)
                            for b in (meta.get("postTokenBalances") or [])
                            if b.get("owner") == pubkey and b.get("mint") == target_mint
                        )
                        if post_tok - pre_tok > 0:
                            actual_tokens = post_tok - pre_tok
        except Exception:
            pass

        return {
            "confirmed": True,
            "explorer_url": f"https://solscan.io/tx/{sig}",
            "actual_tokens_received": actual_tokens,
            "actual_sol_spent": actual_sol_spent,
            "actual_sol_received": actual_sol_received,
            "fee_lamports": fee_lamports,
        }

    def _sign_and_send_in_browser(self, tx_b64, side="BUY"):
        if self.is_tab_alive():
            if self.auto_confirm and self._cdp_port:
                threading.Thread(
                    target=self._auto_click_phantom_popup_cdp,
                    args=(("Confirm", "Approve"), 25.0),
                    daemon=True,
                ).start()
            elif self.auto_confirm and self._pw_ctx:
                threading.Thread(
                    target=self._auto_click_phantom_popup_pw,
                    args=(("Confirm", "Approve"), 25.0),
                    daemon=True,
                ).start()
            return self.submit_sign_job_to_browser(tx_b64, side=side, timeout_sec=45.0)

        return None, "Phantom browser tab is not connected. Click 'Connect Phantom Wallet' in Chrome first."

    def execute_swap(self, side, mint, amount_raw, slippage_bps=150, dry_run=False):
        """
        Execute a live swap through Jupiter v6 + Phantom browser extension popup.
        """
        with self._lock:
            if not self.connected_once or not self.wallet_address:
                return {"ok": False, "error": "Phantom wallet is not connected."}
            if not dry_run and (self.is_read_only or not self.signing_available):
                return {"ok": False, "error": "Wallet is read-only; browser signing unavailable."}

            input_mint = WSOL_MINT if side.upper() == "BUY" else mint
            output_mint = mint if side.upper() == "BUY" else WSOL_MINT

            swap_pkg, err = self.fetch_jupiter_swap_tx(
                input_mint=input_mint,
                output_mint=output_mint,
                amount_raw=amount_raw,
                slippage_bps=slippage_bps,
            )
            if err or not swap_pkg:
                self.last_error = err or "Failed to prepare Jupiter swap"
                return {"ok": False, "error": self.last_error}

            quote = swap_pkg["quote"]
            if dry_run:
                return {
                    "ok": True,
                    "dry_run": True,
                    "signature": f"DRYRUN_{int(time.time())}_{mint[:6]}",
                    "explorer_url": "",
                    "in_amount": int(quote.get("inAmount", amount_raw)),
                    "out_amount": int(quote.get("outAmount", 0)),
                    "api": swap_pkg["api"],
                }

            sig, send_err = self._sign_and_send_in_browser(swap_pkg["swapTransaction"], side=side)
            if send_err or not sig:
                self.last_error = send_err or "Phantom signing failed"
                return {"ok": False, "error": self.last_error}

            verified = self.confirm_and_verify_transaction(sig, target_mint=mint, user_pubkey=self.wallet_address)
            if not verified.get("confirmed"):
                self.last_error = verified.get("error") or f"Tx {sig} failed confirmation"
                return {
                    "ok": False,
                    "signature": sig,
                    "explorer_url": verified.get("explorer_url", f"https://solscan.io/tx/{sig}"),
                    "error": self.last_error,
                }

            self.last_tx_sig = sig
            self._save_session()
            self.refresh_sol_balance()
            return {
                "ok": True,
                "dry_run": False,
                "signature": sig,
                "explorer_url": verified["explorer_url"],
                "in_amount": int(quote.get("inAmount", amount_raw)),
                "out_amount": int(quote.get("outAmount", 0)),
                "actual_tokens_out": verified.get("actual_tokens_received"),
                "actual_sol_delta": verified.get("actual_sol_spent") or verified.get("actual_sol_received"),
                "fee_lamports": verified.get("fee_lamports"),
            }

    def _cleanup_pw(self):
        try:
            if self._pw_ctx:
                self._pw_ctx.close()
        except Exception:
            pass
        try:
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
        self._pw_ctx = None
        self._pw = None

    def close(self):
        with self._lock:
            self._cleanup_pw()
            if self._cdp_proc is not None:
                try:
                    self._cdp_proc.terminate()
                    self._cdp_proc.wait(timeout=3)
                except Exception:
                    try:
                        self._cdp_proc.kill()
                    except Exception:
                        pass
                self._cdp_proc = None
            self._cdp_port = None
            self._driver = None
            self._relay.stop()
            self.status = "paired_idle" if (self.connected_once and self.wallet_address) else "disconnected"
