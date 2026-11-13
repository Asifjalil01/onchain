#!/usr/bin/env python3
"""
Pure-Python Phantom Wallet Browser-Once Automation Bridge.

How "Browser Once" works:
1. The bot launches a persistent Chromium profile (`phantom_profile/`).
2. On first setup ("Connect Phantom Once"), you unlock your Phantom extension
   and approve the dApp connection ONCE inside that persistent browser profile.
3. Because cookies, IndexedDB, extension vault state, and trusted dApp origins
   persist in `phantom_profile/`, subsequent bot runs and live trades reuse the
   already-connected browser session and automatically trigger & confirm Phantom
   swap popups via Playwright Python or pure-Python Chrome DevTools Protocol (CDP).
"""
import base64
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

from http_client import HttpSession
from realtime_stream import PureWebSocket, WSOL_MINT

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None

PHANTOM_EXT_ID = "bfnaelmomeimhlpmgjnjophhpkkoljpa"
JUP_QUOTE_URLS = [
    "https://api.jup.ag/swap/v1",
    "https://quote-api.jup.ag/v6",
]
SOLANA_RPC_URLS = [
    "https://api.mainnet-beta.solana.com",
    "https://solana-rpc.publicnode.com",
]
LAMPORTS_PER_SOL = 1_000_000_000


def find_chromium_binary(custom_path=""):
    """Locate Chrome/Chromium binary across Linux, macOS, Windows, and Playwright cache."""
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


def find_phantom_extension(custom_path=""):
    """Locate an unpacked Phantom browser extension directory if installed locally."""
    if custom_path and os.path.isdir(custom_path):
        if os.path.isfile(os.path.join(custom_path, "manifest.json")):
            return custom_path
        # Check versioned subfolder
        subs = sorted(glob.glob(os.path.join(custom_path, "*")), reverse=True)
        for s in subs:
            if os.path.isfile(os.path.join(s, "manifest.json")):
                return s

    search_roots = []
    sys_name = platform.system().lower()
    home = os.path.expanduser("~")
    if sys_name == "windows":
        la = os.environ.get("LOCALAPPDATA", "")
        search_roots.extend([
            os.path.join(la, "Google", "Chrome", "User Data", "Default", "Extensions", PHANTOM_EXT_ID),
            os.path.join(la, "BraveSoftware", "Brave-Browser", "User Data", "Default", "Extensions", PHANTOM_EXT_ID),
        ])
    elif sys_name == "darwin":
        search_roots.extend([
            os.path.join(home, "Library/Application Support/Google/Chrome/Default/Extensions", PHANTOM_EXT_ID),
            os.path.join(home, "Library/Application Support/BraveSoftware/Brave-Browser/Default/Extensions", PHANTOM_EXT_ID),
        ])
    else:
        search_roots.extend([
            os.path.join(home, ".config/google-chrome/Default/Extensions", PHANTOM_EXT_ID),
            os.path.join(home, ".config/chromium/Default/Extensions", PHANTOM_EXT_ID),
            os.path.join(home, ".config/BraveSoftware/Brave-Browser/Default/Extensions", PHANTOM_EXT_ID),
        ])

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


class PhantomBrowserBridge:
    """
    Browser-Once Phantom automation controller.
    Persists user profile & wallet authorization in `profile_dir` so Phantom
    only needs to be unlocked/connected once, then automates trade signing.
    """

    def __init__(self, profile_dir="phantom_profile", extension_path="", auto_confirm=True,
                 headless=False, rpc_urls=None, jupiter_api_key="", log_fn=None):
        self.profile_dir = os.path.abspath(profile_dir)
        self.extension_path = extension_path
        self.auto_confirm = auto_confirm
        self.headless = headless
        self.rpc_urls = list(rpc_urls or SOLANA_RPC_URLS)
        self.jupiter_api_key = jupiter_api_key
        self._log = log_fn or (lambda m: None)

        self._http = HttpSession()
        self._lock = threading.RLock()
        self._session_file = os.path.join(self.profile_dir, "phantom_session.json")

        # Runtime handles
        self._pw = None
        self._pw_ctx = None
        self._cdp_proc = None
        self._cdp_port = None
        self._driver = None  # "playwright" | "cdp" | None

        # Cached state
        self.status = "disconnected"  # disconnected | launching | awaiting_unlock | connected | error
        self.wallet_address = ""
        self.sol_balance = 0.0
        self.connected_once = False
        self.last_error = ""
        self.last_tx_sig = ""
        self.last_checked_at = 0.0
        self.browser_pid = None

        self._load_session()

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
            "updated_at": time.time(),
        }
        tmp = self._session_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp, self._session_file)

    def get_status(self):
        with self._lock:
            ext_found = find_phantom_extension(self.extension_path)
            bin_found = find_chromium_binary()
            return {
                "status": self.status,
                "connected_once": self.connected_once,
                "wallet_address": self.wallet_address,
                "sol_balance": round(self.sol_balance, 6),
                "auto_confirm": self.auto_confirm,
                "profile_dir": self.profile_dir,
                "extension_detected": bool(ext_found),
                "extension_path": ext_found or self.extension_path,
                "chromium_detected": bool(bin_found),
                "chromium_path": bin_found or "",
                "driver": self._driver or ("playwright" if sync_playwright else "cdp_stdlib"),
                "browser_active": self.is_browser_running(),
                "last_tx_sig": self.last_tx_sig,
                "last_error": self.last_error,
            }

    def is_browser_running(self):
        if self._pw_ctx is not None:
            return True
        if self._cdp_proc is not None and self._cdp_proc.poll() is None:
            return True
        return False

    def refresh_sol_balance(self, wallet=None):
        addr = wallet or self.wallet_address
        if not addr:
            return 0.0
        body = {"jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [addr]}
        for rpc in self.rpc_urls:
            try:
                r = self._http.post(rpc, json=body, timeout=5)
                if r.status_code != 200:
                    continue
                val = ((r.json().get("result") or {}).get("value"))
                if val is not None:
                    self.sol_balance = float(val) / LAMPORTS_PER_SOL
                    self.last_checked_at = time.time()
                    return self.sol_balance
            except Exception:
                continue
        return self.sol_balance

    def launch_persistent_browser(self, open_url="https://jup.ag", headless=None):
        """
        Launch the persistent browser profile (`profile_dir`) with Phantom extension
        loaded (when available). Uses Playwright Python if installed, otherwise
        pure-Python Chromium CDP subprocess + PureWebSocket.
        """
        with self._lock:
            if self.is_browser_running():
                return True, f"Browser already running ({self._driver})"

            os.makedirs(self.profile_dir, exist_ok=True)
            ext_dir = find_phantom_extension(self.extension_path)
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
                use_headless = False  # Extensions require full browser UI unless headless=new

            self.status = "launching"
            self.last_error = ""

            # Prefer Playwright if installed and not forced headless-shell without extension
            if sync_playwright is not None and not use_headless:
                try:
                    self._pw = sync_playwright().start()
                    self._pw_ctx = self._pw.chromium.launch_persistent_context(
                        self.profile_dir,
                        headless=use_headless,
                        args=args,
                    )
                    self._driver = "playwright"
                    page = self._pw_ctx.pages[0] if self._pw_ctx.pages else self._pw_ctx.new_page()
                    page.goto(open_url, wait_until="domcontentloaded", timeout=20000)
                    self._log(f"Phantom persistent browser launched via Playwright ({self.profile_dir})")
                    return True, "Launched persistent Playwright browser"
                except Exception as e:
                    self._cleanup_pw()
                    self._log(f"Playwright launch fallback to CDP: {e}")

            # Pure-Python CDP fallback
            chrome_bin = find_chromium_binary()
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
                self._cdp_proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                self._cdp_port = port
                self.browser_pid = self._cdp_proc.pid
                # Wait for CDP /json/version endpoint
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
                    self.last_error = "Chromium launched but CDP port did not respond."
                    return False, self.last_error

                self._driver = "cdp"
                self._log(f"Persistent browser launched via pure-Python CDP on port {port}")
                return True, f"Launched persistent CDP browser (port {port})"
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

    def _cdp_main_target(self):
        targets = self._cdp_targets()
        for t in targets:
            if t.get("type") == "page" and not str(t.get("url", "")).startswith("chrome-extension://"):
                return t
        for t in targets:
            if t.get("type") == "page":
                return t
        return None

    def _auto_click_phantom_popup_cdp(self, keywords=("Connect", "Approve", "Confirm"), timeout=15.0):
        """Watch CDP targets for Phantom notification popup and click confirmation button."""
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
                                return 'waiting:' + btns.map(b => b.innerText).join('|');
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
        return False, "No Phantom confirmation popup clicked before timeout"

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
        return False, "No Phantom confirmation popup clicked before timeout"

    def connect_once(self, wait_unlock_sec=45, wallet_hint=""):
        """
        Browser-Once connection handshake.
        - If Phantom extension is present in the persistent profile, navigates to jup.ag
          and connects `window.phantom.solana`, clicking 'Connect' automatically if needed.
        - If user supplies a watch/verified wallet address during headless/manual setup,
          records the persistent profile binding cleanly so live quote & RPC checks work.
        """
        ok, msg = self.launch_persistent_browser(open_url="https://jup.ag")
        if not ok:
            return False, msg

        self.status = "awaiting_unlock"
        if wallet_hint and len(wallet_hint.strip()) >= 32:
            self.wallet_address = wallet_hint.strip()
            self.connected_once = True
            self.status = "connected"
            self.refresh_sol_balance()
            self._save_session()
            self._log(f"Phantom persistent profile paired with wallet {self.wallet_address[:6]}...{self.wallet_address[-4:]}")
            return True, f"Connected persistent profile ({self.wallet_address[:6]}...)"

        # Attempt live provider connection inside persistent browser
        if self._driver == "playwright" and self._pw_ctx:
            page = self._pw_ctx.pages[0] if self._pw_ctx.pages else self._pw_ctx.new_page()
            deadline = time.time() + min(wait_unlock_sec, 12)
            while time.time() < deadline:
                try:
                    has_phantom = page.evaluate("() => Boolean(window.phantom && window.phantom.solana)")
                    if has_phantom:
                        # Try silent trusted connect first
                        pub = page.evaluate("""async () => {
                            try {
                                const resp = await window.phantom.solana.connect({ onlyIfTrusted: true });
                                return resp.publicKey.toString();
                            } catch (e) {
                                return null;
                            }
                        }""")
                        if not pub:
                            # Trigger interactive connect + auto-click Phantom popup
                            threading.Thread(
                                target=self._auto_click_phantom_popup_pw,
                                args=(("Connect", "Approve"), 12.0),
                                daemon=True,
                            ).start()
                            pub = page.evaluate("""async () => {
                                const resp = await window.phantom.solana.connect();
                                return resp.publicKey.toString();
                            }""")
                        if pub:
                            self.wallet_address = pub
                            self.connected_once = True
                            self.status = "connected"
                            self.refresh_sol_balance()
                            self._save_session()
                            self._log(f"Phantom connected (Browser Once): {pub}")
                            return True, f"Phantom connected: {pub}"
                except Exception as e:
                    self.last_error = str(e)
                time.sleep(0.8)

        elif self._driver == "cdp":
            target = self._cdp_main_target()
            if target and target.get("webSocketDebuggerUrl"):
                client = _CDPPageClient(target["webSocketDebuggerUrl"])
                try:
                    has_phantom = client.eval_js("Boolean(window.phantom && window.phantom.solana)")
                    if has_phantom:
                        pub = client.eval_js("""(async () => {
                            try {
                                const r = await window.phantom.solana.connect({ onlyIfTrusted: true });
                                return r.publicKey.toString();
                            } catch (e) { return null; }
                        })()""")
                        if not pub:
                            threading.Thread(
                                target=self._auto_click_phantom_popup_cdp,
                                args=(("Connect", "Approve"), 12.0),
                                daemon=True,
                            ).start()
                            pub = client.eval_js("""(async () => {
                                const r = await window.phantom.solana.connect();
                                return r.publicKey.toString();
                            })()""")
                        if pub:
                            self.wallet_address = pub
                            self.connected_once = True
                            self.status = "connected"
                            self.refresh_sol_balance()
                            self._save_session()
                            self._log(f"Phantom connected via CDP (Browser Once): {pub}")
                            return True, f"Phantom connected: {pub}"
                except Exception as e:
                    self.last_error = str(e)
                finally:
                    client.close()

        # Browser profile is running & ready for user to unlock Phantom once
        ext_found = find_phantom_extension(self.extension_path)
        if not ext_found:
            self.status = "awaiting_unlock"
            self.last_error = (
                "Persistent browser profile is active, but unpacked Phantom extension was not found automatically. "
                "Set Phantom extension folder in Settings or supply your Phantom public address for quote/RPC verification."
            )
            return False, self.last_error

        self.status = "awaiting_unlock"
        return False, "Browser opened — unlock Phantom extension once, then click Connect again."

    def fetch_jupiter_swap_tx(self, input_mint, output_mint, amount_raw, slippage_bps=200, user_pubkey=None):
        """Fetch live Jupiter quote + serialized swap transaction (base64)."""
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
                        "prioritizationFeeLamports": "auto",
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

    def confirm_signature(self, sig, timeout_sec=30.0):
        """Poll Solana RPC until transaction signature is confirmed or finalized."""
        deadline = time.time() + timeout_sec
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getSignatureStatuses",
            "params": [[sig], {"searchTransactionHistory": True}],
        }
        while time.time() < deadline:
            for rpc in self.rpc_urls:
                try:
                    r = self._http.post(rpc, json=body, timeout=5)
                    if r.status_code != 200:
                        continue
                    val = ((r.json().get("result") or {}).get("value") or [None])[0]
                    if isinstance(val, dict):
                        if val.get("err"):
                            return False, f"On-chain error: {val['err']}"
                        status = val.get("confirmationStatus")
                        if status in ("confirmed", "finalized"):
                            return True, status
                except Exception:
                    continue
            time.sleep(1.0)
        return False, "Confirmation timeout"

    def _sign_and_send_in_browser(self, tx_b64):
        """
        Invoke `window.phantom.solana.signAndSendTransaction` inside the persistent
        Phantom-connected browser session and auto-confirm the Phantom popup.
        """
        if not self.is_browser_running():
            ok, msg = self.launch_persistent_browser(open_url="https://jup.ag")
            if not ok:
                return None, msg

        js_sign = f"""
        (async () => {{
            const provider = window.phantom && window.phantom.solana;
            if (!provider) throw new Error('Phantom provider not injected in page');
            if (!provider.isConnected) {{
                await provider.connect({{ onlyIfTrusted: true }});
            }}
            const bin = atob({json.dumps(tx_b64)});
            const bytes = new Uint8Array(bin.length);
            for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
            let txObj;
            if (window.solanaWeb3 && window.solanaWeb3.VersionedTransaction) {{
                txObj = window.solanaWeb3.VersionedTransaction.deserialize(bytes);
            }} else {{
                txObj = {{ serialize: () => bytes }};
            }}
            const res = await provider.signAndSendTransaction(txObj);
            return res.signature || String(res);
        }})()
        """

        if self._driver == "playwright" and self._pw_ctx:
            page = self._pw_ctx.pages[0] if self._pw_ctx.pages else self._pw_ctx.new_page()
            if self.auto_confirm:
                threading.Thread(
                    target=self._auto_click_phantom_popup_pw,
                    args=(("Confirm", "Approve"), 20.0),
                    daemon=True,
                ).start()
            try:
                sig = page.evaluate(js_sign)
                return sig, None
            except Exception as e:
                return None, str(e)

        if self._driver == "cdp":
            target = self._cdp_main_target()
            if not target or not target.get("webSocketDebuggerUrl"):
                return None, "No active CDP browser page"
            client = _CDPPageClient(target["webSocketDebuggerUrl"])
            try:
                if self.auto_confirm:
                    threading.Thread(
                        target=self._auto_click_phantom_popup_cdp,
                        args=(("Confirm", "Approve"), 20.0),
                        daemon=True,
                    ).start()
                sig = client.eval_js(js_sign)
                return sig, None
            except Exception as e:
                return None, str(e)
            finally:
                client.close()

        return None, "No browser driver active"

    def execute_swap(self, side, mint, amount_raw, slippage_bps=200, dry_run=False):
        """
        Execute a live swap through Jupiter + Phantom Browser-Once automation.
        side: 'BUY' (WSOL -> mint) or 'SELL' (mint -> WSOL)
        amount_raw: lamports (for BUY) or raw token units (for SELL)
        """
        with self._lock:
            if not self.connected_once or not self.wallet_address:
                return {"ok": False, "error": "Phantom is not connected. Run 'Connect Phantom (Once)' first."}

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
                    "in_amount": int(quote.get("inAmount", amount_raw)),
                    "out_amount": int(quote.get("outAmount", 0)),
                    "api": swap_pkg["api"],
                }

            sig, send_err = self._sign_and_send_in_browser(swap_pkg["swapTransaction"])
            if send_err or not sig:
                self.last_error = send_err or "Phantom signing failed"
                return {"ok": False, "error": self.last_error}

            confirmed, status_msg = self.confirm_signature(sig, timeout_sec=30.0)
            if not confirmed:
                self.last_error = f"Tx {sig}: {status_msg}"
                return {"ok": False, "signature": sig, "error": self.last_error}

            self.last_tx_sig = sig
            self._save_session()
            self.refresh_sol_balance()
            return {
                "ok": True,
                "dry_run": False,
                "signature": sig,
                "in_amount": int(quote.get("inAmount", amount_raw)),
                "out_amount": int(quote.get("outAmount", 0)),
                "confirmation": status_msg,
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
            self.status = "paired_idle" if (self.connected_once and self.wallet_address) else "disconnected"
