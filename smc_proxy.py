#!/usr/bin/env python3
"""
smc_proxy.py v3.0 — SMC Detector Production Proxy
===================================================
Runs locally AND on cloud platforms (Render, Railway, Fly.io).

Local:   python smc_proxy.py
Cloud:   Set environment variables, push to GitHub, deploy.

Environment Variables (set in Render/Railway dashboard):
  PORT         — server port (Render sets this automatically)
  ANT_KEY      — Anthropic API key (sk-ant-...)
  KITE_KEY     — Kite API key
  KITE_TOKEN   — Kite access token (refresh daily or automate)

Endpoints:
  GET  /health
  GET  /yahoo?symbol=RELIANCE.NS&range=1y&interval=1d
  GET  /kite?url=<kite_api_path>
  POST /claude   { "prompt": "..." }
  GET  /?url=<any>   (legacy passthrough)
"""

import http.server
import urllib.request
import urllib.parse
import urllib.error
import json
import time
import threading
import os
import sys

# ─────────────────────────────────────────────────────
# CONFIG — reads from environment variables first,
#          falls back to hardcoded values for local use
# ─────────────────────────────────────────────────────
PORT       = int(os.environ.get('PORT',       8000))
ANT_KEY    = os.environ.get('ANT_KEY',        '')   # Anthropic API key
KITE_KEY   = os.environ.get('KITE_KEY',       '')   # Kite API key
KITE_TOKEN = os.environ.get('KITE_TOKEN',     '')   # Kite access token

# ── For LOCAL use only — paste here if not using env vars ──
# Leave blank when deploying to cloud (use env vars instead)
LOCAL_ANT_KEY    = ''   # e.g. 'sk-ant-api03-...'
LOCAL_KITE_KEY   = ''   # e.g. 'u664cda77q2cf7ft'
LOCAL_KITE_TOKEN = ''   # e.g. 'accesskw5m...'
# ──────────────────────────────────────────────────────────

# Merge: env var wins over local hardcoded value
if not ANT_KEY    and LOCAL_ANT_KEY:    ANT_KEY    = LOCAL_ANT_KEY
if not KITE_KEY   and LOCAL_KITE_KEY:   KITE_KEY   = LOCAL_KITE_KEY
if not KITE_TOKEN and LOCAL_KITE_TOKEN: KITE_TOKEN = LOCAL_KITE_TOKEN

# ─────────────────────────────────────────────────────
# YAHOO FINANCE — crumb/cookie auth
# Yahoo requires a session cookie + crumb token for all API calls
# This proxy handles that automatically
# ─────────────────────────────────────────────────────
_lock      = threading.Lock()
_crumb     = None
_opener    = None
_crumb_ts  = 0
CRUMB_TTL  = 3600  # refresh every hour

YF_HEADERS = {
    'User-Agent':
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/120.0.0.0 Safari/537.36',
    'Accept':          'application/json,text/html,*/*',
    'Accept-Language': 'en-US,en;q=0.9',
    'Referer':         'https://finance.yahoo.com/',
}


def refresh_crumb():
    global _crumb, _opener, _crumb_ts
    print('[Yahoo] Refreshing crumb...', flush=True)
    try:
        jar    = urllib.request.HTTPCookieProcessor()
        opener = urllib.request.build_opener(jar)
        # Step 1: visit finance.yahoo.com to get session cookie
        req1 = urllib.request.Request(
            'https://finance.yahoo.com/', headers=YF_HEADERS)
        opener.open(req1, timeout=12)
        # Step 2: fetch crumb token
        req2 = urllib.request.Request(
            'https://query1.finance.yahoo.com/v1/test/getcrumb',
            headers=YF_HEADERS)
        with opener.open(req2, timeout=12) as r:
            crumb = r.read().decode('utf-8').strip()
        if crumb and len(crumb) > 3:
            with _lock:
                _crumb, _opener, _crumb_ts = crumb, opener, time.time()
            print(f'[Yahoo] Crumb OK: {crumb[:8]}...', flush=True)
            return True
        print('[Yahoo] Crumb response empty', flush=True)
    except Exception as e:
        print(f'[Yahoo] Crumb refresh failed: {e}', flush=True)
    return False


def get_crumb_and_opener():
    with _lock:
        if _crumb and (time.time() - _crumb_ts) < CRUMB_TTL:
            return _crumb, _opener
    refresh_crumb()
    with _lock:
        return _crumb, _opener


def yahoo_fetch(symbol, range_='1y', interval='1d'):
    crumb, opener = get_crumb_and_opener()
    if not crumb or not opener:
        return None, 'crumb_failed'

    is_nse = symbol.endswith('.NS') or symbol.endswith('.BO')
    region = 'IN' if is_nse else 'US'
    lang   = 'en-IN' if is_nse else 'en-US'

    url = (
        f'https://query1.finance.yahoo.com/v8/finance/chart/{symbol}'
        f'?range={range_}&interval={interval}'
        f'&includePrePost=false&events=div%2Csplit'
        f'&region={region}&lang={lang}'
        f'&corsDomain=finance.yahoo.com'
        f'&crumb={urllib.parse.quote(crumb)}'
    )

    try:
        req = urllib.request.Request(url, headers=YF_HEADERS)
        with opener.open(req, timeout=15) as r:
            data = r.read()
        print(f'[Yahoo] {symbol} OK — {len(data)} bytes', flush=True)
        return data, None
    except urllib.error.HTTPError as e:
        body = e.read()[:200].decode('utf-8', errors='replace')
        print(f'[Yahoo] {symbol} HTTP{e.code}: {body}', flush=True)
        if e.code == 401:
            # Crumb expired — force refresh on next request
            with _lock:
                global _crumb
                _crumb = None
        return None, f'HTTP{e.code}'
    except Exception as e:
        print(f'[Yahoo] {symbol} error: {e}', flush=True)
        return None, str(e)


# ─────────────────────────────────────────────────────
# REQUEST HANDLER
# ─────────────────────────────────────────────────────
class SMCHandler(http.server.BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        # Suppress noisy access logs; only print errors
        msg = fmt % args
        if any(c in msg for c in ['40', '50']):
            print(f'[HTTP] {msg}', flush=True)

    # ── CORS preflight ────────────────────────────────
    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    # ── GET endpoints ─────────────────────────────────
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        qs     = urllib.parse.parse_qs(parsed.query)
        path   = parsed.path.rstrip('/')

        # /health
        if path == '/health':
            self._json(200, {
                'status':    'ok',
                'version':   '3.0',
                'port':      PORT,
                'crumb':     bool(_crumb),
                'kite':      bool(KITE_KEY and KITE_TOKEN),
                'claude':    bool(ANT_KEY),
                'endpoints': ['/health', '/yahoo', '/kite', '/claude', '/?url='],
            })
            return

        # /yahoo
        if path == '/yahoo':
            sym  = qs.get('symbol',   ['RELIANCE.NS'])[0]
            rng  = qs.get('range',    ['1y'])[0]
            ivl  = qs.get('interval', ['1d'])[0]
            data, err = yahoo_fetch(sym, rng, ivl)
            if data:
                self.send_response(200)
                self._cors()
                self.send_header('Content-Type',   'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            else:
                self._json(502, {'error': err, 'symbol': sym})
            return

        # /kite
        if path == '/kite':
            target = qs.get('url', [None])[0]
            if not target:
                self._json(400, {'error': 'missing ?url= parameter'})
                return
            # Use server-side Kite credentials if available,
            # otherwise fall back to per-request headers (local use)
            api_key   = KITE_KEY   or self.headers.get('X-SMC-Key',   '')
            api_token = KITE_TOKEN or self.headers.get('X-SMC-Token', '')
            req = urllib.request.Request(target)
            req.add_header('X-Kite-Version', '3')
            if api_key and api_token:
                req.add_header('Authorization', f'token {api_key}:{api_token}')
            try:
                with urllib.request.urlopen(req, timeout=12) as r:
                    body = r.read()
                self.send_response(200)
                self._cors()
                self.send_header('Content-Type',   'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                print(f'[Kite] {target[:70]} OK', flush=True)
            except urllib.error.HTTPError as e:
                self._json(e.code, {'error': f'Kite HTTP{e.code}'})
            except Exception as e:
                self._json(502, {'error': str(e)})
            return

        # Legacy /?url= passthrough (backward compatible)
        target = qs.get('url', [None])[0]
        if target:
            api_key   = KITE_KEY   or self.headers.get('X-SMC-Key',   '')
            api_token = KITE_TOKEN or self.headers.get('X-SMC-Token', '')
            req = urllib.request.Request(target, headers=YF_HEADERS)
            req.add_header('X-Kite-Version', '3')
            if api_key and api_token:
                req.add_header('Authorization', f'token {api_key}:{api_token}')
            try:
                with urllib.request.urlopen(req, timeout=12) as r:
                    body = r.read()
                self.send_response(200)
                self._cors()
                self.send_header('Content-Type',   'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                self._json(502, {'error': str(e)})
            return

        self._json(404, {
            'error':     'unknown endpoint',
            'available': ['/health', '/yahoo', '/kite', '/claude', '/?url='],
        })

    # ── POST endpoints ────────────────────────────────
    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path   = parsed.path.rstrip('/')

        # /claude — Anthropic AI relay
        if path == '/claude':
            # API key: server env var wins; fallback to request header
            ant_key = ANT_KEY or self.headers.get('X-ANT-KEY', '')
            if not ant_key:
                self._json(400, {
                    'error': 'Anthropic API key not configured. '
                             'Set ANT_KEY env var on server or paste key in dashboard.'
                })
                return

            length   = int(self.headers.get('Content-Length', 0))
            raw_body = self.rfile.read(length) if length else b'{}'
            try:
                body_json = json.loads(raw_body)
                prompt    = body_json.get('prompt', '').strip()
            except Exception:
                self._json(400, {'error': 'Invalid JSON body'})
                return

            if not prompt:
                self._json(400, {'error': 'Missing prompt in request body'})
                return

            payload = json.dumps({
                'model':      'claude-sonnet-4-20250514',
                'max_tokens': 800,
                'messages':   [{'role': 'user', 'content': prompt}],
            }).encode('utf-8')

            req = urllib.request.Request(
                'https://api.anthropic.com/v1/messages',
                data=payload,
                headers={
                    'Content-Type':      'application/json',
                    'x-api-key':         ant_key,
                    'anthropic-version': '2023-06-01',
                }
            )
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    resp = json.loads(r.read())
                text = resp.get('content', [{}])[0].get('text', 'No response')
                self._json(200, {'text': text})
                print(f'[Claude] OK — {len(text)} chars', flush=True)
            except urllib.error.HTTPError as e:
                err_body = e.read().decode('utf-8', errors='replace')[:300]
                print(f'[Claude] HTTP{e.code}: {err_body}', flush=True)
                self._json(e.code, {'error': err_body})
            except Exception as e:
                print(f'[Claude] Error: {e}', flush=True)
                self._json(502, {'error': str(e)})
            return

        self._json(404, {'error': 'unknown POST endpoint'})

    # ── Helpers ───────────────────────────────────────
    def _json(self, code, obj):
        body = json.dumps(obj, indent=2).encode('utf-8')
        self.send_response(code)
        self._cors()
        self.send_header('Content-Type',   'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _cors(self):
        origin = self.headers.get('Origin', '*')
        self.send_header('Access-Control-Allow-Origin',
                         origin if origin else '*')
        self.send_header('Access-Control-Allow-Methods',
                         'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers',
                         'Accept, Content-Type, '
                         'X-SMC-Key, X-SMC-Token, X-ANT-KEY, Cache-Control')
        self.send_header('Access-Control-Allow-Credentials', 'false')
        self.send_header('Vary', 'Origin')


# ─────────────────────────────────────────────────────
# STARTUP
# ─────────────────────────────────────────────────────
if __name__ == '__main__':
    print('=' * 52, flush=True)
    print('  SMC Proxy v3.0', flush=True)
    print(f'  http://localhost:{PORT}', flush=True)
    print('=' * 52, flush=True)
    print(f'  Yahoo Finance  : crumb auth auto-managed', flush=True)
    print(f'  Kite API       : {"✓ configured" if KITE_KEY else "✗ not set (set KITE_KEY + KITE_TOKEN)"}', flush=True)
    print(f'  Claude AI      : {"✓ configured" if ANT_KEY  else "✗ not set (set ANT_KEY)"}', flush=True)
    print('=' * 52, flush=True)
    print('  Ctrl+C to stop\n', flush=True)

    # Pre-fetch Yahoo crumb in background so first scan is fast
    threading.Thread(target=refresh_crumb, daemon=True).start()

    server = http.server.HTTPServer(('0.0.0.0', PORT), SMCHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\n  Proxy stopped.', flush=True)
        server.shutdown()
