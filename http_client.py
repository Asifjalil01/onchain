#!/usr/bin/env python3
"""
Lightweight HTTP JSON client.
Uses `requests.Session` when available, with a zero-dependency Python stdlib
`urllib.request` fallback so the bot runs on any Python 3.10+ environment.
"""
import json
import ssl
import urllib.error
import urllib.parse
import urllib.request

try:
    import requests as _requests
except ImportError:
    _requests = None


class _StdlibResponse:
    def __init__(self, status_code, body_bytes):
        self.status_code = status_code
        self._body = body_bytes

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return json.loads(self._body.decode("utf-8", errors="replace"))

    @property
    def text(self):
        return self._body.decode("utf-8", errors="replace")


class HttpSession:
    def __init__(self):
        self._req_session = _requests.Session() if _requests is not None else None
        self._ssl_ctx = ssl.create_default_context()

    def get(self, url, params=None, headers=None, timeout=6):
        if self._req_session is not None:
            return self._req_session.get(url, params=params, headers=headers, timeout=timeout)
        full_url = url
        if params:
            qs = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
            sep = "&" if "?" in full_url else "?"
            if qs:
                full_url = f"{full_url}{sep}{qs}"
        req = urllib.request.Request(full_url, headers=headers or {}, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=self._ssl_ctx) as resp:
                return _StdlibResponse(resp.status, resp.read())
        except urllib.error.HTTPError as e:
            return _StdlibResponse(e.code, e.read() if e.fp else b"")

    def post(self, url, json_body=None, json=None, headers=None, timeout=6):
        payload = json if json is not None else json_body
        if self._req_session is not None:
            return self._req_session.post(url, json=payload, headers=headers, timeout=timeout)
        hdrs = dict(headers or {})
        hdrs.setdefault("Content-Type", "application/json")
        data = globals()["json"].dumps(payload).encode("utf-8") if payload is not None else b""
        req = urllib.request.Request(url, data=data, headers=hdrs, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=self._ssl_ctx) as resp:
                return _StdlibResponse(resp.status, resp.read())
        except urllib.error.HTTPError as e:
            return _StdlibResponse(e.code, e.read() if e.fp else b"")


# Alias for drop-in use
globals()["json"] = json
