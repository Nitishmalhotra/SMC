#!/usr/bin/env python3
"""
smc_proxy.py v3.1 — Production Ready
====================================

Runs locally AND on cloud platforms (Render, Railway, Fly.io).

Local:
    python smc_proxy.py

Cloud:
    Set environment variables in Render/Railway dashboard.

Environment Variables:
    PORT
    ANT_KEY
    KITE_KEY
    KITE_TOKEN

Endpoints:
    GET  /
    GET  /health
    GET  /yahoo?symbol=RELIANCE.NS&range=1y&interval=1d
    GET  /kite?url=<kite_api_path>
    POST /claude
    GET  /?url=<any>
"""

import http.server
import urllib.request
import urllib.parse
import urllib.error
import json
import time
import threading
import os

# ─────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────

PORT = int(os.environ.get("PORT", 10000))

ANT_KEY = os.environ.get("ANT_KEY", "")
KITE_KEY = os.environ.get("KITE_KEY", "")
KITE_TOKEN = os.environ.get("KITE_TOKEN", "")

# Optional local fallback keys
LOCAL_ANT_KEY = ""
LOCAL_KITE_KEY = ""
LOCAL_KITE_TOKEN = ""

if not ANT_KEY and LOCAL_ANT_KEY:
    ANT_KEY = LOCAL_ANT_KEY

if not KITE_KEY and LOCAL_KITE_KEY:
    KITE_KEY = LOCAL_KITE_KEY

if not KITE_TOKEN and LOCAL_KITE_TOKEN:
    KITE_TOKEN = LOCAL_KITE_TOKEN

# ─────────────────────────────────────────────────────
# YAHOO FINANCE
# ─────────────────────────────────────────────────────

_lock = threading.Lock()
_crumb = None
_opener = None
_crumb_ts = 0

CRUMB_TTL = 3600

YF_HEADERS = {
    "User-Agent":
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36",

    "Accept": "application/json,text/html,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://finance.yahoo.com/",
}


def refresh_crumb():
    global _crumb, _opener, _crumb_ts

    print("[Yahoo] Refreshing crumb...", flush=True)

    try:
        jar = urllib.request.HTTPCookieProcessor()
        opener = urllib.request.build_opener(jar)

        # Step 1 — Get Yahoo session cookie
        req1 = urllib.request.Request(
            "https://finance.yahoo.com/",
            headers=YF_HEADERS
        )

        opener.open(req1, timeout=12)

        # Step 2 — Get crumb token
        req2 = urllib.request.Request(
            "https://query1.finance.yahoo.com/v1/test/getcrumb",
            headers=YF_HEADERS
        )

        with opener.open(req2, timeout=12) as r:
            crumb = r.read().decode("utf-8").strip()

        if crumb and len(crumb) > 3:
            with _lock:
                _crumb = crumb
                _opener = opener
                _crumb_ts = time.time()

            print(f"[Yahoo] Crumb OK: {crumb[:8]}...", flush=True)
            return True

        print("[Yahoo] Empty crumb response", flush=True)

    except Exception as e:
        print(f"[Yahoo] Crumb refresh failed: {e}", flush=True)

    return False


def get_crumb_and_opener():
    with _lock:
        if _crumb and (time.time() - _crumb_ts) < CRUMB_TTL:
            return _crumb, _opener

    refresh_crumb()

    with _lock:
        return _crumb, _opener


def yahoo_fetch(symbol, range_="1y", interval="1d"):

    # Try crumb-based fetch first
    crumb, opener = get_crumb_and_opener()

    is_nse = symbol.endswith(".NS") or symbol.endswith(".BO")

    region = "IN" if is_nse else "US"
    lang = "en-IN" if is_nse else "en-US"

    base_url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        f"?range={range_}"
        f"&interval={interval}"
        f"&includePrePost=false"
        f"&events=div%2Csplit"
        f"&region={region}"
        f"&lang={lang}"
        f"&corsDomain=finance.yahoo.com"
    )

    # Attempt with crumb first
    if crumb and opener:
        try:

            url = f"{base_url}&crumb={urllib.parse.quote(crumb)}"

            req = urllib.request.Request(url, headers=YF_HEADERS)

            with opener.open(req, timeout=15) as r:
                data = r.read()

            print(f"[Yahoo] {symbol} OK (crumb)", flush=True)

            return data, None

        except urllib.error.HTTPError as e:

            body = e.read()[:200].decode(
                "utf-8",
                errors="replace"
            )

            print(
                f"[Yahoo] {symbol} HTTP{e.code}: {body}",
                flush=True
            )

            if e.code == 401:
                with _lock:
                    global _crumb
                    _crumb = None

        except Exception as e:
            print(f"[Yahoo] crumb fetch failed: {e}", flush=True)

    # Fallback WITHOUT crumb
    try:

        req = urllib.request.Request(base_url, headers=YF_HEADERS)

        with urllib.request.urlopen(req, timeout=15) as r:
            data = r.read()

        print(f"[Yahoo] {symbol} OK (fallback)", flush=True)

        return data, None

    except Exception as e:
        print(f"[Yahoo] fallback failed: {e}", flush=True)
        return None, str(e)


# ─────────────────────────────────────────────────────
# HTTP HANDLER
# ─────────────────────────────────────────────────────

class SMCHandler(http.server.BaseHTTPRequestHandler):

    # ─────────────────────────────────────────────
    # Cleaner logs
    # ─────────────────────────────────────────────
    def log_message(self, fmt, *args):
        msg = fmt % args

        if any(code in msg for code in ["40", "50"]):
            print(f"[HTTP] {msg}", flush=True)

    # ─────────────────────────────────────────────
    # HEAD SUPPORT (Render health checks)
    # ─────────────────────────────────────────────
    def do_HEAD(self):

        self.send_response(200)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ─────────────────────────────────────────────
    # OPTIONS (CORS preflight)
    # ─────────────────────────────────────────────
    def do_OPTIONS(self):

        self.send_response(200)
        self._cors()
        self.end_headers()

    # ─────────────────────────────────────────────
    # GET
    # ─────────────────────────────────────────────
    def do_GET(self):

        parsed = urllib.parse.urlparse(self.path)

        qs = urllib.parse.parse_qs(parsed.query)

        path = parsed.path.rstrip("/")

        # ─────────────────────────────────────────
        # ROOT /
        # ─────────────────────────────────────────
        if path == "":

            html = f"""
            <html>
            <head>
                <title>SMC Proxy v3.1</title>
            </head>

            <body style="font-family:Arial;padding:40px">

                <h2>✅ SMC Proxy v3.1 Running</h2>

                <p>Backend Status: ONLINE</p>

                <h3>Available Endpoints</h3>

                <ul>
                    <li>/health</li>
                    <li>/yahoo?symbol=RELIANCE.NS</li>
                    <li>/kite?url=...</li>
                    <li>/claude</li>
                    <li>/?url=...</li>
                </ul>

                <hr>

                <h3>Environment Status</h3>

                <ul>
                    <li>Yahoo Finance: ✅</li>
                    <li>Kite API: {"✅ Configured" if KITE_KEY else "❌ Missing"}</li>
                    <li>Claude AI: {"✅ Configured" if ANT_KEY else "❌ Missing"}</li>
                </ul>

            </body>
            </html>
            """

            body = html.encode("utf-8")

            self.send_response(200)
            self._cors()
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()

            self.wfile.write(body)

            return

        # ─────────────────────────────────────────
        # /health
        # ─────────────────────────────────────────
        if path == "/health":

            self._json(200, {
                "status": "ok",
                "version": "3.1",
                "port": PORT,
                "crumb": bool(_crumb),
                "kite": bool(KITE_KEY and KITE_TOKEN),
                "claude": bool(ANT_KEY),
                "endpoints": [
                    "/",
                    "/health",
                    "/yahoo",
                    "/kite",
                    "/claude",
                    "/?url="
                ]
            })

            return

        # ─────────────────────────────────────────
        # /yahoo
        # ─────────────────────────────────────────
        if path == "/yahoo":

            symbol = qs.get("symbol", ["RELIANCE.NS"])[0]

            range_ = qs.get("range", ["1y"])[0]

            interval = qs.get("interval", ["1d"])[0]

            data, err = yahoo_fetch(
                symbol,
                range_,
                interval
            )

            if data:

                self.send_response(200)
                self._cors()

                self.send_header(
                    "Content-Type",
                    "application/json"
                )

                self.send_header(
                    "Content-Length",
                    str(len(data))
                )

                self.end_headers()

                self.wfile.write(data)

            else:

                self._json(502, {
                    "error": err,
                    "symbol": symbol
                })

            return

        # ─────────────────────────────────────────
        # /kite
        # ─────────────────────────────────────────
        if path == "/kite":

            target = qs.get("url", [None])[0]

            if not target:
                self._json(400, {
                    "error": "missing ?url= parameter"
                })
                return

            api_key = (
                KITE_KEY
                or self.headers.get("X-SMC-Key", "")
            )

            api_token = (
                KITE_TOKEN
                or self.headers.get("X-SMC-Token", "")
            )

            req = urllib.request.Request(target)

            req.add_header("X-Kite-Version", "3")

            if api_key and api_token:
                req.add_header(
                    "Authorization",
                    f"token {api_key}:{api_token}"
                )

            try:

                with urllib.request.urlopen(req, timeout=12) as r:
                    body = r.read()

                self.send_response(200)
                self._cors()

                self.send_header(
                    "Content-Type",
                    "application/json"
                )

                self.send_header(
                    "Content-Length",
                    str(len(body))
                )

                self.end_headers()

                self.wfile.write(body)

                print(
                    f"[Kite] {target[:70]} OK",
                    flush=True
                )

            except urllib.error.HTTPError as e:

                self._json(e.code, {
                    "error": f"Kite HTTP{e.code}"
                })

            except Exception as e:

                self._json(502, {
                    "error": str(e)
                })

            return

        # ─────────────────────────────────────────
        # Legacy /?url=
        # ─────────────────────────────────────────
        target = qs.get("url", [None])[0]

        if target:

            api_key = (
                KITE_KEY
                or self.headers.get("X-SMC-Key", "")
            )

            api_token = (
                KITE_TOKEN
                or self.headers.get("X-SMC-Token", "")
            )

            req = urllib.request.Request(
                target,
                headers=YF_HEADERS
            )

            req.add_header("X-Kite-Version", "3")

            if api_key and api_token:
                req.add_header(
                    "Authorization",
                    f"token {api_key}:{api_token}"
                )

            try:

                with urllib.request.urlopen(req, timeout=12) as r:
                    body = r.read()

                self.send_response(200)
                self._cors()

                self.send_header(
                    "Content-Type",
                    "application/json"
                )

                self.send_header(
                    "Content-Length",
                    str(len(body))
                )

                self.end_headers()

                self.wfile.write(body)

            except Exception as e:

                self._json(502, {
                    "error": str(e)
                })

            return

        # ─────────────────────────────────────────
        # Unknown endpoint
        # ─────────────────────────────────────────
        self._json(404, {
            "error": "unknown endpoint",
            "available": [
                "/",
                "/health",
                "/yahoo",
                "/kite",
                "/claude",
                "/?url="
            ]
        })

    # ─────────────────────────────────────────────
    # POST
    # ─────────────────────────────────────────────
    def do_POST(self):

        parsed = urllib.parse.urlparse(self.path)

        path = parsed.path.rstrip("/")

        # ─────────────────────────────────────────
        # /claude
        # ─────────────────────────────────────────
        if path == "/claude":

            ant_key = (
                ANT_KEY
                or self.headers.get("X-ANT-KEY", "")
            )

            if not ant_key:

                self._json(400, {
                    "error":
                        "Anthropic API key missing. "
                        "Set ANT_KEY env var."
                })

                return

            length = int(
                self.headers.get("Content-Length", 0)
            )

            raw_body = (
                self.rfile.read(length)
                if length else b"{}"
            )

            try:

                body_json = json.loads(raw_body)

                prompt = body_json.get(
                    "prompt",
                    ""
                ).strip()

            except Exception:

                self._json(400, {
                    "error": "Invalid JSON body"
                })

                return

            if not prompt:

                self._json(400, {
                    "error": "Missing prompt"
                })

                return

            payload = json.dumps({
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 800,
                "messages": [
                    {
                        "role": "user",
                        "content": prompt
                    }
                ]
            }).encode("utf-8")

            req = urllib.request.Request(
                "https://api.anthropic.com/v1/messages",
                data=payload,
                headers={
                    "Content-Type":
                        "application/json",

                    "x-api-key":
                        ant_key,

                    "anthropic-version":
                        "2023-06-01",
                }
            )

            try:

                with urllib.request.urlopen(req, timeout=30) as r:
                    resp = json.loads(r.read())

                text = resp.get(
                    "content",
                    [{}]
                )[0].get(
                    "text",
                    "No response"
                )

                self._json(200, {
                    "text": text
                })

                print(
                    f"[Claude] OK — {len(text)} chars",
                    flush=True
                )

            except urllib.error.HTTPError as e:

                err_body = e.read().decode(
                    "utf-8",
                    errors="replace"
                )[:300]

                print(
                    f"[Claude] HTTP{e.code}: {err_body}",
                    flush=True
                )

                self._json(e.code, {
                    "error": err_body
                })

            except Exception as e:

                print(
                    f"[Claude] Error: {e}",
                    flush=True
                )

                self._json(502, {
                    "error": str(e)
                })

            return

        # Unknown POST
        self._json(404, {
            "error": "unknown POST endpoint"
        })

    # ─────────────────────────────────────────────
    # JSON helper
    # ─────────────────────────────────────────────
    def _json(self, code, obj):

        body = json.dumps(
            obj,
            indent=2
        ).encode("utf-8")

        self.send_response(code)

        self._cors()

        self.send_header(
            "Content-Type",
            "application/json"
        )

        self.send_header(
            "Content-Length",
            str(len(body))
        )

        self.end_headers()

        self.wfile.write(body)

    # ─────────────────────────────────────────────
    # CORS helper
    # ─────────────────────────────────────────────
    def _cors(self):

        origin = self.headers.get("Origin", "*")

        self.send_header(
            "Access-Control-Allow-Origin",
            origin if origin else "*"
        )

        self.send_header(
            "Access-Control-Allow-Methods",
            "GET, POST, OPTIONS, HEAD"
        )

        self.send_header(
            "Access-Control-Allow-Headers",
            "Accept, Content-Type, "
            "X-SMC-Key, X-SMC-Token, "
            "X-ANT-KEY, Cache-Control"
        )

        self.send_header(
            "Access-Control-Allow-Credentials",
            "false"
        )

        self.send_header(
            "Vary",
            "Origin"
        )


# ─────────────────────────────────────────────────────
# START SERVER
# ─────────────────────────────────────────────────────

if __name__ == "__main__":

    print("=" * 55, flush=True)

    print("  SMC Proxy v3.1", flush=True)

    print(f"  http://localhost:{PORT}", flush=True)

    print("=" * 55, flush=True)

    print(
        "  Yahoo Finance  : crumb auth auto-managed",
        flush=True
    )

    print(
        f"  Kite API       : "
        f"{'✓ configured' if KITE_KEY else '✗ not set'}",
        flush=True
    )

    print(
        f"  Claude AI      : "
        f"{'✓ configured' if ANT_KEY else '✗ not set'}",
        flush=True
    )

    print("=" * 55, flush=True)

    print("  Ctrl+C to stop\n", flush=True)

    # Preload Yahoo crumb
    threading.Thread(
        target=refresh_crumb,
        daemon=True
    ).start()

    server = http.server.HTTPServer(
        ("0.0.0.0", PORT),
        SMCHandler
    )

    try:
        server.serve_forever()

    except KeyboardInterrupt:

        print("\n  Proxy stopped.", flush=True)

        server.shutdown()
