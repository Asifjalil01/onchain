#!/usr/bin/env python3
"""
Unit & integration tests for the pure-Python Solana Sniper Bot.
Run: python3 -m unittest test_bot.py -v
"""
import io
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import bot
import engine
import phantom_bridge
import realtime_stream
import web_dashboard


class TestFiltersAndPaperAccount(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.cfg = dict(bot.CONFIG)
        self.cfg["state_file"] = os.path.join(self.tmpdir, "state.json")
        self.cfg["trades_csv"] = os.path.join(self.tmpdir, "trades.csv")
        self.cfg["log_rejects"] = False

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_ultra_early_grace_skips_liquidity_floor(self):
        now_ms = int(time.time() * 1000)
        pair = {
            "pairCreatedAt": now_ms - 4000,  # 4s old (< 10s grace)
            "priceUsd": "0.00012",
            "liquidity": {"usd": 50},  # below 300 floor, allowed in grace
            "txns": {"m5": {"buys": 1, "sells": 0}},
            "priceChange": {"m5": 15},
        }
        ok, code, _ = bot.passes_filters(pair, self.cfg, now_ms)
        self.assertTrue(ok, f"Expected grace pass, got {code}")

    def test_after_grace_enforces_liquidity_and_buys(self):
        now_ms = int(time.time() * 1000)
        pair = {
            "pairCreatedAt": now_ms - 30000,  # 30s old (> 10s grace)
            "priceUsd": "0.00012",
            "liquidity": {"usd": 120},
            "txns": {"m5": {"buys": 5, "sells": 1}},
            "priceChange": {"m5": 15},
        }
        ok, code, _ = bot.passes_filters(pair, self.cfg, now_ms)
        self.assertFalse(ok)
        self.assertEqual(code, "low liquidity")

    def test_paper_buy_and_take_profit_sell(self):
        acct = bot.PaperAccount(self.cfg)
        mint = "So11111111111111111111111111111111111111112"
        bought = acct.buy(mint, "TEST", 1.0, meta={"dex": "raydium", "source": "gecko", "age_s": 5})
        self.assertTrue(bought)
        self.assertIn(mint, acct.state["positions"])
        self.assertAlmostEqual(acct.state["cash"], 45.0, places=4)

        # Price rises +80% -> triggers 50% TP
        exits = acct.check_exits({mint: 1.80})
        self.assertEqual(exits, 1)
        self.assertEqual(len(acct.state["positions"]), 0)
        self.assertEqual(acct.state["closed"], 1)
        self.assertEqual(acct.state["wins"], 1)
        self.assertGreater(acct.state["total_pnl"], 0)
        self.assertTrue(os.path.exists(self.cfg["trades_csv"]))


class TestRealtimeStreaming(unittest.TestCase):
    def test_rfc6455_frame_encode_decode(self):
        payload = json.dumps({"method": "subscribeNewToken", "data": "x" * 200})
        frame = realtime_stream.encode_ws_frame(payload, opcode=0x1, mask=True)
        buf = io.BytesIO(frame)

        def recv_exact(n):
            b = buf.read(n)
            if len(b) < n:
                raise EOFError()
            return b

        fin, opcode, decoded = realtime_stream.decode_ws_frame_from_reader(recv_exact)
        self.assertTrue(fin)
        self.assertEqual(opcode, 0x1)
        self.assertEqual(decoded.decode("utf-8"), payload)

    def test_pumpportal_event_parsing_and_queue(self):
        mgr = realtime_stream.RealtimeStreamManager()
        mgr.sol_price_usd = 200.0
        mint = "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU"
        ev = mgr.feed_event({
            "mint": mint,
            "symbol": "PUMPX",
            "name": "Pump X",
            "txType": "create",
            "initialBuy": 1.0,
            "vSolInBondingCurve": 30.0,
            "vTokensInBondingCurve": 1000000.0,
            "marketCapSol": 35.0,
        })
        self.assertIsNotNone(ev)
        self.assertEqual(ev["kind"], "new_token")
        # 30 / 1,000,000 * 200 = 0.006 USD
        self.assertAlmostEqual(ev["price_usd"], 0.006, places=6)

        mints = mgr.pop_new_mints()
        self.assertEqual(mints, [(mint, "pumpportal_ws")])
        live_prices = mgr.get_live_prices()
        self.assertAlmostEqual(live_prices[mint], 0.006, places=6)
        stats = mgr.get_stats()
        self.assertEqual(stats["new_tokens"], 1)


class TestPhantomBridgeAndLiveAccount(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.profile_dir = os.path.join(self.tmpdir, "phantom_profile")
        self.cfg = dict(bot.CONFIG)
        self.cfg["live_state_file"] = os.path.join(self.tmpdir, "live_state.json")
        self.cfg["live_trades_csv"] = os.path.join(self.tmpdir, "live_trades.csv")
        self.cfg["live_trade_size_sol"] = 0.05
        self.cfg["live_max_sol_per_trade"] = 0.25
        self.cfg["live_dry_run"] = True

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_persistent_session_reload(self):
        bridge = phantom_bridge.PhantomBrowserBridge(profile_dir=self.profile_dir)
        bridge.wallet_address = "5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d"
        bridge.connected_once = True
        bridge.last_tx_sig = "sig123456"
        bridge._save_session()

        bridge2 = phantom_bridge.PhantomBrowserBridge(profile_dir=self.profile_dir)
        self.assertTrue(bridge2.connected_once)
        self.assertEqual(bridge2.wallet_address, bridge.wallet_address)
        self.assertEqual(bridge2.status, "paired_idle")

    def test_live_account_dry_run_swap_and_safety_cap(self):
        bridge = phantom_bridge.PhantomBrowserBridge(profile_dir=self.profile_dir)
        bridge.wallet_address = "5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d"
        bridge.connected_once = True
        bridge.sol_balance = 1.5

        # Stub Jupiter quote call so test is deterministic offline
        def fake_swap(side, mint, amount_raw, slippage_bps=200, dry_run=False):
            return {
                "ok": True,
                "dry_run": dry_run,
                "signature": f"DRYRUN_{side}_{mint[:6]}",
                "in_amount": amount_raw,
                "out_amount": 500_000_000 if side == "BUY" else 60_000_000,
            }

        bridge.execute_swap = fake_swap
        acct = bot.LiveAccount(self.cfg, bridge)

        # Exceeding safety cap blocks trade
        self.cfg["live_trade_size_sol"] = 0.9
        ok, why = acct.can_open()
        self.assertFalse(ok)
        self.assertIn("cap", why)

        # Valid size opens and closes position
        self.cfg["live_trade_size_sol"] = 0.05
        mint = "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU"
        self.assertTrue(acct.buy(mint, "LIVECOIN", 0.01, meta={"dex": "raydium", "source": "pumpportal_ws", "age_s": 2}))
        self.assertIn(mint, acct.state["positions"])
        acct.sell(mint, 0.02, "take_profit_50")
        self.assertEqual(len(acct.state["positions"]), 0)
        self.assertEqual(acct.state["closed"], 1)
        self.assertTrue(os.path.exists(self.cfg["live_trades_csv"]))


class TestSettingsAndWebDashboard(unittest.TestCase):
    def setUp(self):
        self.orig_cfg = dict(bot.CONFIG)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), web_dashboard.DashboardHandler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        bot.CONFIG.update(self.orig_cfg)

    def _req(self, path, method="GET", body=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        hdrs = {"Content-Type": "application/json"} if data is not None else {}
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8")

    def test_dashboard_html_and_api_state(self):
        status, html_body = self._req("/")
        self.assertEqual(status, 200)
        self.assertIn("Solana Sniper Bot", html_body)

        status, state_raw = self._req("/api/state")
        self.assertEqual(status, 200)
        state = json.loads(state_raw)
        self.assertIn("mode", state)
        self.assertIn("phantom", state)
        self.assertIn("stream", state)

    def test_settings_validation_rejects_bad_values(self):
        status, resp_raw = self._req("/api/settings", method="POST", body={"trade_size_usd": -10})
        self.assertEqual(status, 400)
        resp = json.loads(resp_raw)
        self.assertFalse(resp["ok"])
        self.assertTrue(len(resp["errors"]) > 0)

    def test_stream_simulate_endpoint(self):
        status, resp_raw = self._req("/api/stream/simulate", method="POST", body={
            "mint": "9xQeWvG816bUx9EPjHmaT23yvVM2ZWbrrpZb9PusVFin",
            "symbol": "SIMX",
            "txType": "create",
            "initialBuy": 1,
            "vSolInBondingCurve": 25,
            "vTokensInBondingCurve": 500000,
        })
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(resp_raw)["ok"])

    def test_wallet_connect_balance_live_confirm_gate_and_disconnect(self):
        eng = web_dashboard.get_engine()
        orig_refresh = eng.bridge.refresh_sol_balance
        eng.bridge.refresh_sol_balance = lambda *a, **kw: eng.bridge.sol_balance
        try:
            eng.disconnect_phantom()

            # 1) Wrong confirmation text rejected
            status, raw = self._req("/api/live/confirm", method="POST", body={"confirmation": "YES"})
            self.assertEqual(status, 400)
            self.assertFalse(json.loads(raw)["ok"])

            # 2) Valid text but no wallet connected -> blocked
            status, raw = self._req("/api/live/confirm", method="POST", body={"confirmation": "ENABLE LIVE"})
            self.assertEqual(status, 400)
            self.assertIn("No Phantom wallet", json.loads(raw)["message"])

            # 3) Read-only wallet -> blocked
            status, _ = self._req("/api/wallets/connect", method="POST", body={
                "address": "5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d",
                "signing_available": False,
                "is_read_only": True,
            })
            self.assertEqual(status, 200)
            status, raw = self._req("/api/live/confirm", method="POST", body={"confirmation": "ENABLE LIVE"})
            self.assertEqual(status, 400)
            self.assertIn("read-only", json.loads(raw)["message"])

            # 4) Signing wallet connected but balance below trade (0.05) + gas reserve (0.02) -> blocked
            eng.bridge.sol_balance = 0.06
            status, _ = self._req("/api/wallets/connect", method="POST", body={
                "address": "5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d",
                "signing_available": True,
                "is_read_only": False,
            })
            self.assertEqual(status, 200)
            status, raw = self._req("/api/live/confirm", method="POST", body={"confirmation": "ENABLE LIVE"})
            self.assertEqual(status, 400)
            self.assertIn("Insufficient SOL", json.loads(raw)["message"])

            # 5) Sufficient balance + ENABLE LIVE -> unlocks Live Mode
            eng.bridge.sol_balance = 0.5
            status, raw = self._req("/api/live/confirm", method="POST", body={"confirmation": "enable live"})
            self.assertEqual(status, 200)
            resp = json.loads(raw)
            self.assertTrue(resp["ok"])
            self.assertEqual(resp["mode"], "live")
            self.assertTrue(resp["live_mode_confirmed"])

            # 6) Balance endpoint returns USD + reserve telemetry
            status, bal_raw = self._req("/api/wallets/balance")
            self.assertEqual(status, 200)
            bal = json.loads(bal_raw)
            self.assertEqual(bal["sol_balance"], 0.5)
            self.assertEqual(bal["gas_reserved_sol"], 0.02)

            # 7) Disconnect disarms Live Mode back to paper
            status, _ = self._req("/api/wallets/disconnect", method="POST", body={})
            self.assertEqual(status, 200)
            self.assertEqual(eng.mode, "paper")
            self.assertFalse(eng.live_mode_confirmed)
        finally:
            eng.bridge.refresh_sol_balance = orig_refresh


class TestExistingChromeRelayAndGuiImports(unittest.TestCase):
    def test_chrome_profile_discovery_from_local_state(self):
        tmpdir = tempfile.mkdtemp()
        try:
            with open(os.path.join(tmpdir, "Local State"), "w", encoding="utf-8") as f:
                json.dump({
                    "profile": {
                        "info_cache": {
                            "Default": {"name": "Personal"},
                            "Profile 1": {"name": "Trading"},
                        }
                    }
                }, f)
            profs = phantom_bridge.list_chrome_profiles(user_data_dir=tmpdir)
            self.assertEqual([p["dir"] for p in profs], ["Default", "Profile 1"])
            self.assertEqual(profs[1]["name"], "Trading")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_local_phantom_relay_connect_and_sign_roundtrip(self):
        tmpdir = tempfile.mkdtemp()
        bridge = phantom_bridge.PhantomBrowserBridge(profile_dir=tmpdir, connection_mode="existing_chrome")
        bridge.refresh_sol_balance = lambda *a, **kw: 1.25
        try:
            url = bridge._relay.url()
            base = url.rsplit("/", 1)[0]

            # Simulate existing Chrome tab reporting connected Phantom wallet
            req = urllib.request.Request(
                f"{base}/api/wallets/connect",
                data=json.dumps({
                    "address": "5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d",
                    "signing_available": True,
                    "is_read_only": False,
                }).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=4) as r:
                self.assertEqual(r.status, 200)
            self.assertTrue(bridge.connected_once)
            self.assertTrue(bridge.signing_available)
            self.assertEqual(bridge.status, "connected")

            # Simulate signing job submitted by Python & completed by existing Chrome tab
            def simulate_browser_tab():
                time.sleep(0.15)
                with urllib.request.urlopen(f"{base}/api/relay/next", timeout=4) as r:
                    job = json.loads(r.read().decode("utf-8"))
                self.assertIn("id", job)
                res_req = urllib.request.Request(
                    f"{base}/api/relay/result",
                    data=json.dumps({"id": job["id"], "ok": True, "signature": "5TestRelaySigXYZ"}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                urllib.request.urlopen(res_req, timeout=4).close()

            t = threading.Thread(target=simulate_browser_tab, daemon=True)
            t.start()
            sig, err = bridge.submit_sign_job_to_browser("AQAB", side="BUY", timeout_sec=5.0)
            self.assertIsNone(err)
            self.assertEqual(sig, "5TestRelaySigXYZ")
        finally:
            bridge.close()
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_confirm_and_verify_parsed_transaction(self):
        tmpdir = tempfile.mkdtemp()
        try:
            bridge = phantom_bridge.PhantomBrowserBridge(profile_dir=tmpdir)
            pubkey = "5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d"
            mint = "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU"

            class _FakeResp:
                def __init__(self, data):
                    self.status_code = 200
                    self._data = data

                def json(self):
                    return self._data

            class _FakeHttp:
                def post(self, url, json=None, timeout=5):
                    method = (json or {}).get("method")
                    if method == "getSignatureStatuses":
                        return _FakeResp({"result": {"value": [{"confirmationStatus": "confirmed", "err": None}]}})
                    if method == "getParsedTransaction":
                        return _FakeResp({
                            "result": {
                                "transaction": {"message": {"accountKeys": [{"pubkey": pubkey}]}},
                                "meta": {
                                    "fee": 5000,
                                    "preBalances": [1_000_000_000],
                                    "postBalances": [949_995_000],
                                    "preTokenBalances": [],
                                    "postTokenBalances": [
                                        {"owner": pubkey, "mint": mint, "uiTokenAmount": {"uiAmount": 420.5}}
                                    ],
                                },
                            }
                        })
                    return _FakeResp({})

            bridge._http = _FakeHttp()
            verified = bridge.confirm_and_verify_transaction("5TxSig123", target_mint=mint, user_pubkey=pubkey)
            self.assertTrue(verified["confirmed"])
            self.assertEqual(verified["explorer_url"], "https://solscan.io/tx/5TxSig123")
            self.assertAlmostEqual(verified["actual_tokens_received"], 420.5, places=4)
            self.assertAlmostEqual(verified["actual_sol_spent"], 0.05, places=6)
            self.assertEqual(verified["fee_lamports"], 5000)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_gui_module_has_required_imports(self):
        import gui
        self.assertTrue(hasattr(gui, "os"))
        self.assertTrue(hasattr(gui, "time"))
        self.assertTrue(hasattr(gui, "bot"))
        self.assertTrue(hasattr(gui, "App"))


if __name__ == "__main__":
    unittest.main()

