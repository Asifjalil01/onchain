#!/usr/bin/env python3
"""
Pure-Python Real-Time WebSocket Streaming Engine for Solana Sniper Bot.

Provides:
1. `PureWebSocket`: Zero-dependency RFC 6455 WebSocket client (ws:// & wss://)
   built purely on Python stdlib (`socket`, `ssl`, `hashlib`, `struct`).
2. `RealtimeStreamManager`: Multi-stream real-time ingestion from:
   - PumpPortal WebSocket (`wss://pumpportal.fun/api/data`):
     sub-second new token launches (`subscribeNewToken`) + real-time trade ticks
     (`subscribeTokenTrade`) for instant price updates on held positions.
   - Solana RPC WebSocket (`wss://api.mainnet-beta.solana.com`):
     `logsSubscribe` on Raydium AMM v4, Pump.fun, and Meteora pool creation events.
"""
import base64
import hashlib
import json
import os
import queue
import socket
import ssl
import struct
import threading
import time
import urllib.parse
from collections import deque

from http_client import HttpSession

PUMPPORTAL_WS = "wss://pumpportal.fun/api/data"
SOLANA_WS_URLS = [
    "wss://api.mainnet-beta.solana.com",
    "wss://solana-rpc.publicnode.com",
]

RAYDIUM_AMM_V4 = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
PUMPFUN_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
METEORA_DLMM = "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo"
WSOL_MINT = "So11111111111111111111111111111111111111112"

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class WebSocketError(Exception):
    pass


def encode_ws_frame(payload, opcode=0x1, mask=True):
    """Encode an RFC 6455 WebSocket frame (client frames must be masked)."""
    if isinstance(payload, str):
        data = payload.encode("utf-8")
    else:
        data = bytes(payload)
    first_byte = 0x80 | (opcode & 0x0F)
    length = len(data)
    mask_bit = 0x80 if mask else 0x00

    header = bytearray([first_byte])
    if length < 126:
        header.append(mask_bit | length)
    elif length < 65536:
        header.append(mask_bit | 126)
        header.extend(struct.pack("!H", length))
    else:
        header.append(mask_bit | 127)
        header.extend(struct.pack("!Q", length))

    if mask:
        mask_key = os.urandom(4)
        header.extend(mask_key)
        masked = bytes(b ^ mask_key[i % 4] for i, b in enumerate(data))
        return bytes(header) + masked
    return bytes(header) + data


def decode_ws_frame_from_reader(recv_exact):
    """
    Decode one RFC 6455 WebSocket frame using a `recv_exact(n)` callable.
    Returns (fin: bool, opcode: int, payload: bytes).
    """
    head = recv_exact(2)
    b1, b2 = head[0], head[1]
    fin = bool(b1 & 0x80)
    opcode = b1 & 0x0F
    masked = bool(b2 & 0x80)
    length = b2 & 0x7F

    if length == 126:
        length = struct.unpack("!H", recv_exact(2))[0]
    elif length == 127:
        length = struct.unpack("!Q", recv_exact(8))[0]

    mask_key = recv_exact(4) if masked else None
    payload = recv_exact(length) if length > 0 else b""
    if mask_key:
        payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
    return fin, opcode, payload


class PureWebSocket:
    """Zero-dependency blocking RFC 6455 WebSocket client over stdlib socket/ssl."""

    def __init__(self, url, timeout=10.0, headers=None):
        self.url = url
        self.timeout = timeout
        self.extra_headers = headers or {}
        self.sock = None
        self.closed = True
        self._send_lock = threading.Lock()
        self._buf = bytearray()

    def connect(self):
        parsed = urllib.parse.urlparse(self.url)
        if parsed.scheme not in ("ws", "wss"):
            raise WebSocketError(f"Unsupported scheme: {parsed.scheme}")
        host = parsed.hostname
        port = parsed.port or (443 if parsed.scheme == "wss" else 80)
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"

        raw_sock = socket.create_connection((host, port), timeout=self.timeout)
        if parsed.scheme == "wss":
            ctx = ssl.create_default_context()
            self.sock = ctx.wrap_socket(raw_sock, server_hostname=host)
        else:
            self.sock = raw_sock
        self.sock.settimeout(self.timeout)

        key = base64.b64encode(os.urandom(16)).decode("ascii")
        host_hdr = f"{host}:{port}" if parsed.port else host
        req_lines = [
            f"GET {path} HTTP/1.1",
            f"Host: {host_hdr}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {key}",
            "Sec-WebSocket-Version: 13",
            "User-Agent: solana-launch-sniper/3.0",
        ]
        for k, v in self.extra_headers.items():
            req_lines.append(f"{k}: {v}")
        req_lines.extend(["", ""])
        self.sock.sendall("\r\n".join(req_lines).encode("ascii"))

        resp_data = bytearray()
        while b"\r\n\r\n" not in resp_data:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise WebSocketError("Connection closed during WebSocket handshake")
            resp_data.extend(chunk)
            if len(resp_data) > 65536:
                raise WebSocketError("HTTP handshake header too large")

        header_end = resp_data.index(b"\r\n\r\n")
        header_bytes = bytes(resp_data[:header_end])
        leftover = bytes(resp_data[header_end + 4 :])
        if leftover:
            self._buf.extend(leftover)

        lines = header_bytes.decode("latin1").split("\r\n")
        if not lines or " 101 " not in lines[0]:
            raise WebSocketError(f"Handshake rejected: {lines[0] if lines else 'empty'}")

        expected_accept = base64.b64encode(
            hashlib.sha1((key + _WS_GUID).encode("ascii")).digest()
        ).decode("ascii")
        hdrs = {}
        for line in lines[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                hdrs[k.strip().lower()] = v.strip()
        if hdrs.get("sec-websocket-accept") != expected_accept:
            raise WebSocketError("Invalid Sec-WebSocket-Accept from server")

        self.closed = False
        return self

    def _recv_exact(self, n):
        while len(self._buf) < n:
            chunk = self.sock.recv(max(4096, n - len(self._buf)))
            if not chunk:
                raise EOFError("WebSocket socket closed")
            self._buf.extend(chunk)
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    def send_text(self, text):
        if self.closed or not self.sock:
            raise WebSocketError("WebSocket is not connected")
        frame = encode_ws_frame(text, opcode=0x1, mask=True)
        with self._send_lock:
            self.sock.sendall(frame)

    def send_json(self, obj):
        self.send_text(json.dumps(obj))

    def send_pong(self, payload=b""):
        if self.closed or not self.sock:
            return
        frame = encode_ws_frame(payload, opcode=0xA, mask=True)
        with self._send_lock:
            self.sock.sendall(frame)

    def send_ping(self, payload=b"ping"):
        if self.closed or not self.sock:
            return
        frame = encode_ws_frame(payload, opcode=0x9, mask=True)
        with self._send_lock:
            self.sock.sendall(frame)

    def recv(self):
        """Read next complete text/binary message. Automatically replies to pings."""
        fragments = []
        msg_opcode = None
        while True:
            fin, opcode, payload = decode_ws_frame_from_reader(self._recv_exact)
            if opcode == 0x8:  # close
                self.close()
                return None
            if opcode == 0x9:  # ping
                self.send_pong(payload)
                continue
            if opcode == 0xA:  # pong
                continue
            if opcode in (0x1, 0x2):
                msg_opcode = opcode
                fragments.append(payload)
            elif opcode == 0x0:
                fragments.append(payload)
            if fin:
                full = b"".join(fragments)
                if msg_opcode == 0x2:
                    return full
                return full.decode("utf-8", errors="replace")

    def close(self):
        if not self.closed:
            self.closed = True
            try:
                if self.sock:
                    with self._send_lock:
                        self.sock.sendall(encode_ws_frame(b"", opcode=0x8, mask=True))
            except Exception:
                pass
        try:
            if self.sock:
                self.sock.close()
        except Exception:
            pass
        self.sock = None


def parse_pumpportal_event(msg, sol_price_usd=180.0):
    """
    Parse a PumpPortal WebSocket JSON message into a normalized event dict.
    Returns dict with keys: kind ('new_token' | 'trade' | 'info'), mint, symbol,
    price_usd, market_cap_usd, liquidity_usd, tx_type, source, ts, raw.
    """
    if not isinstance(msg, dict):
        return None
    mint = msg.get("mint")
    if not mint or not isinstance(mint, str):
        return None

    tx_type = str(msg.get("txType") or "").lower()
    symbol = str(msg.get("symbol") or msg.get("name") or "?")[:16]
    v_sol = _to_float(msg.get("vSolInBondingCurve"))
    v_tok = _to_float(msg.get("vTokensInBondingCurve"))
    mcap_sol = _to_float(msg.get("marketCapSol"))

    price_usd = None
    if v_sol is not None and v_tok is not None and v_tok > 0 and sol_price_usd > 0:
        # PumpPortal vTokensInBondingCurve is in whole tokens (6 decimals normalized)
        price_sol = v_sol / v_tok
        price_usd = price_sol * sol_price_usd
    elif _to_float(msg.get("solAmount")) and _to_float(msg.get("tokenAmount")):
        sa = _to_float(msg.get("solAmount"))
        ta = _to_float(msg.get("tokenAmount"))
        if ta > 0 and sol_price_usd > 0:
            price_usd = (sa / ta) * sol_price_usd

    kind = "new_token" if (tx_type == "create" or ("initialBuy" in msg and tx_type != "sell")) else "trade"
    return {
        "kind": kind,
        "mint": mint,
        "symbol": symbol,
        "name": str(msg.get("name") or symbol)[:32],
        "tx_type": tx_type or "create",
        "price_usd": price_usd,
        "market_cap_usd": (mcap_sol * sol_price_usd) if mcap_sol else None,
        "v_sol": v_sol,
        "source": "pumpportal_ws",
        "signature": msg.get("signature") or "",
        "ts": time.time(),
    }


def _to_float(val):
    try:
        return float(val) if val is not None else None
    except (TypeError, ValueError):
        return None


class RealtimeStreamManager:
    """
    Manages background WebSocket threads for real-time Solana launches & prices.
    Thread-safe for bot scanner, monitor, Tkinter GUI, and web SSE consumers.
    """

    def __init__(self, log_fn=None, on_new_mint=None, on_price_tick=None):
        self._log = log_fn or (lambda m: None)
        self._on_new_mint = on_new_mint
        self._on_price_tick = on_price_tick
        self._http = HttpSession()

        self._stop = threading.Event()
        self._running = False
        self._lock = threading.RLock()

        self._new_mint_q = queue.Queue(maxsize=2000)
        self._recent_events = deque(maxlen=250)
        self._live_prices = {}  # mint -> (price_usd, ts)
        self._watched_mints = set()
        self._pump_ws = None

        self.sol_price_usd = 180.0
        self._last_sol_fetch = 0.0

        self.pump_status = "disconnected"
        self.solana_status = "disconnected"
        self.msg_count = 0
        self.new_token_count = 0
        self.trade_tick_count = 0
        self.last_event_ts = 0.0
        self._rate_window = deque(maxlen=200)

    def start(self, enable_pumpportal=True, enable_solana_ws=True):
        with self._lock:
            if self._running:
                return
            self._stop.clear()
            self._running = True
        self._refresh_sol_price()
        if enable_pumpportal:
            threading.Thread(target=self._pumpportal_loop, daemon=True).start()
        if enable_solana_ws:
            threading.Thread(target=self._solana_ws_loop, daemon=True).start()
        threading.Thread(target=self._sol_price_loop, daemon=True).start()
        self._log("Real-time WebSocket streaming started (PumpPortal WS + Solana RPC WS).")

    def stop(self):
        with self._lock:
            if not self._running:
                return
            self._running = False
            self._stop.set()
            ws = self._pump_ws
            self._pump_ws = None
        if ws:
            try:
                ws.close()
            except Exception:
                pass
        self.pump_status = "disconnected"
        self.solana_status = "disconnected"

    @property
    def is_running(self):
        return self._running

    def _sol_price_loop(self):
        while not self._stop.is_set():
            if time.time() - self._last_sol_fetch > 30:
                self._refresh_sol_price()
            self._stop.wait(5.0)

    def _refresh_sol_price(self):
        self._last_sol_fetch = time.time()
        try:
            r = self._http.get(f"https://api.dexscreener.com/tokens/v1/solana/{WSOL_MINT}", timeout=4)
            r.raise_for_status()
            data = r.json()
            if isinstance(data, list) and data:
                p = _to_float(data[0].get("priceUsd"))
                if p and p > 1.0:
                    self.sol_price_usd = p
                    return p
        except Exception:
            pass
        return self.sol_price_usd

    def subscribe_token(self, mint):
        if not mint:
            return
        with self._lock:
            added = mint not in self._watched_mints
            self._watched_mints.add(mint)
            ws = self._pump_ws
        if added and ws and not ws.closed:
            try:
                ws.send_json({"method": "subscribeTokenTrade", "keys": [mint]})
            except Exception:
                pass

    def unsubscribe_token(self, mint):
        if not mint:
            return
        with self._lock:
            removed = mint in self._watched_mints
            self._watched_mints.discard(mint)
            ws = self._pump_ws
        if removed and ws and not ws.closed:
            try:
                ws.send_json({"method": "unsubscribeTokenTrade", "keys": [mint]})
            except Exception:
                pass

    def pop_new_mints(self, max_items=60):
        out = []
        while len(out) < max_items:
            try:
                out.append(self._new_mint_q.get_nowait())
            except queue.Empty:
                break
        return out

    def get_live_prices(self, max_age_sec=15.0):
        now_t = time.time()
        with self._lock:
            return {
                m: price
                for m, (price, ts) in self._live_prices.items()
                if now_t - ts <= max_age_sec
            }

    def get_recent_events(self, limit=80):
        with self._lock:
            items = list(self._recent_events)
        return items[-limit:][::-1]

    def get_stats(self):
        now_t = time.time()
        with self._lock:
            while self._rate_window and now_t - self._rate_window[0] > 10.0:
                self._rate_window.popleft()
            mps = len(self._rate_window) / 10.0
            return {
                "running": self._running,
                "pump_status": self.pump_status,
                "solana_status": self.solana_status,
                "msg_count": self.msg_count,
                "new_tokens": self.new_token_count,
                "trade_ticks": self.trade_tick_count,
                "msgs_per_sec": round(mps, 2),
                "sol_price_usd": round(self.sol_price_usd, 2),
                "watched_mints": len(self._watched_mints),
                "last_event_age_sec": round(now_t - self.last_event_ts, 1) if self.last_event_ts else None,
            }

    def _record_event(self, ev):
        now_t = time.time()
        with self._lock:
            self.msg_count += 1
            self.last_event_ts = now_t
            self._rate_window.append(now_t)
            self._recent_events.append(ev)
            if ev["kind"] == "new_token":
                self.new_token_count += 1
            elif ev["kind"] == "trade":
                self.trade_tick_count += 1
            if ev.get("price_usd") and ev["price_usd"] > 0:
                self._live_prices[ev["mint"]] = (ev["price_usd"], now_t)

        if ev["kind"] == "new_token":
            try:
                self._new_mint_q.put_nowait((ev["mint"], ev["source"]))
            except queue.Full:
                pass
            # Automatically watch trade ticks briefly for ultra-early price tracking
            self.subscribe_token(ev["mint"])
            if self._on_new_mint:
                try:
                    self._on_new_mint(ev["mint"], ev)
                except Exception:
                    pass
        if ev.get("price_usd") and self._on_price_tick:
            try:
                self._on_price_tick(ev["mint"], ev["price_usd"], ev)
            except Exception:
                pass

    def feed_event(self, raw_payload):
        """Inject a raw PumpPortal-style payload (used for tests / replay)."""
        ev = parse_pumpportal_event(raw_payload, self.sol_price_usd)
        if ev:
            self._record_event(ev)
        return ev

    def _pumpportal_loop(self):
        backoff = 1.0
        while not self._stop.is_set():
            ws = None
            try:
                self.pump_status = "connecting"
                ws = PureWebSocket(PUMPPORTAL_WS, timeout=15.0).connect()
                with self._lock:
                    self._pump_ws = ws
                    watched = list(self._watched_mints)
                self.pump_status = "connected"
                backoff = 1.0
                ws.send_json({"method": "subscribeNewToken"})
                if watched:
                    ws.send_json({"method": "subscribeTokenTrade", "keys": watched})

                last_ping = time.time()
                while not self._stop.is_set() and not ws.closed:
                    try:
                        raw = ws.recv()
                    except socket.timeout:
                        if time.time() - last_ping >= 15:
                            ws.send_ping()
                            last_ping = time.time()
                        continue
                    if raw is None:
                        break
                    try:
                        payload = json.loads(raw)
                    except Exception:
                        continue
                    ev = parse_pumpportal_event(payload, self.sol_price_usd)
                    if ev:
                        self._record_event(ev)
            except Exception as e:
                if not self._stop.is_set():
                    self.pump_status = f"reconnecting ({e})"
            finally:
                with self._lock:
                    if self._pump_ws is ws:
                        self._pump_ws = None
                if ws:
                    ws.close()
            if not self._stop.is_set():
                self._stop.wait(backoff)
                backoff = min(20.0, backoff * 1.8)

    def _solana_ws_loop(self):
        backoff = 2.0
        idx = 0
        while not self._stop.is_set():
            ws = None
            url = SOLANA_WS_URLS[idx % len(SOLANA_WS_URLS)]
            idx += 1
            try:
                self.solana_status = "connecting"
                ws = PureWebSocket(url, timeout=20.0).connect()
                self.solana_status = "connected"
                backoff = 2.0
                for req_id, prog in enumerate((PUMPFUN_PROGRAM, RAYDIUM_AMM_V4, METEORA_DLMM), start=1):
                    ws.send_json({
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "method": "logsSubscribe",
                        "params": [{"mentions": [prog]}, {"commitment": "processed"}],
                    })
                last_ping = time.time()
                seen_sigs = deque(maxlen=500)
                while not self._stop.is_set() and not ws.closed:
                    try:
                        raw = ws.recv()
                    except socket.timeout:
                        if time.time() - last_ping >= 20:
                            ws.send_ping()
                            last_ping = time.time()
                        continue
                    if raw is None:
                        break
                    try:
                        data = json.loads(raw)
                    except Exception:
                        continue
                    if data.get("method") == "logsNotification":
                        val = ((data.get("params") or {}).get("result") or {}).get("value") or {}
                        logs = val.get("logs") or []
                        sig = val.get("signature") or ""
                        if not sig or sig in seen_sigs:
                            continue
                        joined = " ".join(logs)
                        if any(k in joined for k in ("Instruction: Create", "initialize2", "InitializePool")):
                            seen_sigs.append(sig)
                            ev = {
                                "kind": "pool_log",
                                "mint": sig[:44],
                                "symbol": "POOL_INIT",
                                "name": "On-chain Pool Init",
                                "tx_type": "pool_init",
                                "price_usd": None,
                                "market_cap_usd": None,
                                "v_sol": None,
                                "source": "solana_rpc_ws",
                                "signature": sig,
                                "ts": time.time(),
                            }
                            with self._lock:
                                self.msg_count += 1
                                self.last_event_ts = time.time()
                                self._rate_window.append(self.last_event_ts)
                                self._recent_events.append(ev)
            except Exception as e:
                if not self._stop.is_set():
                    self.solana_status = f"reconnecting ({e})"
            finally:
                if ws:
                    ws.close()
            if not self._stop.is_set():
                self._stop.wait(backoff)
                backoff = min(30.0, backoff * 1.8)
