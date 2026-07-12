"""Fetch per-title Xbox playtime via MSA + XSTS OAuth against Xbox Live.

OpenXBL's free tier does not expose ``minutesPlayed`` in ``/v2/titles`` (the
``stats`` object is a ``sourceVersion``-only stub), so per-title playtime has
to come from Xbox Live's own ``userstats.xboxlive.com/batch`` endpoint. That
endpoint requires the full Microsoft-Account / XBL / XSTS token stack.

We drive the three-hop token exchange ourselves rather than relying on the
``xbox-webapi`` community library, because that library still targets the
legacy ``login.live.com/oauth20_*`` MSA v1 endpoints. Modern Azure "Personal
Microsoft accounts only" app registrations live on the v2 endpoint
(``login.microsoftonline.com/consumers/oauth2/v2.0/...``) — mixing the two
produces a broken consent flow that MS terminates with ``?removed=true``.

Layout:
    - ``login`` (CLI): prompt the user through the browser login once.
    - ``fetch_playtime_minutes(title_ids)``: sync wrapper the platforms module
      calls during ``sync_xbox()``. Returns ``{title_id: minutes}`` for as
      many titles as we could resolve.

Failure modes are deliberately quiet: no tokens on disk, expired refresh
token, Xbox Live outage, or a per-title miss all result in ``{}`` or an
absent map entry rather than a raised exception, so the rest of the sync
still succeeds.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import sqlite3
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
from dotenv import load_dotenv

# The web app calls load_dotenv() in gigalib.app during Flask startup, but
# this module also runs standalone via ``python -m gigalib.xbox_stats login``
# where the env has not been populated yet. Re-loading is safe (no-op if the
# vars are already set).
load_dotenv()

log = logging.getLogger(__name__)

# Public client id of the gigalib-xbox Azure app registration. It's public
# on purpose (no client secret, native app flow) — same pattern the Azure
# CLI and GitHub CLI use to ship a bundled OAuth client. Users can override
# via ``XBOX_CLIENT_ID`` if they'd rather run their own Azure registration.
CLIENT_ID = os.environ.get(
    "XBOX_CLIENT_ID", "2ae078e9-cd54-4370-9869-402f7087ec5a"
)
# Login uses the standard OAuth loopback flow (RFC 8252): we bind a random
# free port on 127.0.0.1 and MS redirects back with the code, so the user
# never has to see or copy a URL. For this to work the Azure app must have
# ``http://localhost`` registered as a Mobile-and-Desktop redirect URI; MS
# then accepts any port at runtime.
SCOPES = "XboxLive.signin XboxLive.offline_access offline_access"

# MSA v2 endpoints (Azure app registrations, personal accounts only).
_AUTHORIZE_URL = (
    "https://login.microsoftonline.com/consumers/oauth2/v2.0/authorize"
)
_TOKEN_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"

# Xbox Live auth chain.
_XBL_USER_URL = "https://user.auth.xboxlive.com/user/authenticate"
_XSTS_URL = "https://xsts.auth.xboxlive.com/xsts/authorize"

# Resolve relative to the project root so the file is found regardless of
# the process CWD (Task Scheduler runs the server with cwd=System32).
TOKENS_PATH = Path(__file__).resolve().parent.parent / "instance" / "xbox_tokens.json"

# Local Xbox app's SQLite cache. Used purely to discover the SCID for each
# titleId — the values themselves come from Xbox Live over the network.
_XBOX_APP_CACHE = (
    Path(os.environ.get("LOCALAPPDATA", ""))
    / "Packages"
    / "Microsoft.GamingApp_8wekyb3d8bbwe"
    / "LocalState"
    / "AsyncCache.db"
)


# ---------------------------------------------------------------------------
# SCID lookup (per-title Xbox Live Service Configuration Id)
# ---------------------------------------------------------------------------
def _load_scid_map() -> dict[str, str]:
    """Return ``{title_id: scid}`` for every title Xbox app has seen locally.

    Source: ``achievements`` scope, keys ``userStats_{xuid}_{titleId}``, value
    JSON contains stat definitions with ``scid``. If the Xbox app isn't
    installed we return an empty map and skip playtime cleanly.
    """
    if not _XBOX_APP_CACHE.exists():
        return {}

    # Copy to temp because the live DB is often WAL-locked by the Xbox app.
    tmp = Path(os.environ.get("TEMP", ".")) / "gigalib_xbox_scid_probe.db"
    try:
        tmp.write_bytes(_XBOX_APP_CACHE.read_bytes())
    except OSError as exc:
        log.debug("could not copy Xbox app cache: %s", exc)
        return {}

    out: dict[str, str] = {}
    try:
        with sqlite3.connect(tmp) as conn:
            rows = conn.execute(
                "SELECT key, value FROM AsyncCache WHERE scope = 'achievements' "
                "AND key LIKE 'userStats_%'"
            ).fetchall()
        for key, value in rows:
            parts = key.split("_")
            if len(parts) < 3:
                continue
            title_id = parts[-1]
            try:
                stats = json.loads(value)
            except (TypeError, ValueError):
                continue
            if not isinstance(stats, list):
                continue
            for stat in stats:
                scid = (stat or {}).get("scid")
                if scid:
                    out[title_id] = scid
                    break
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    return out


# ---------------------------------------------------------------------------
# Token store
# ---------------------------------------------------------------------------
def _load_tokens() -> Optional[dict]:
    if not TOKENS_PATH.exists():
        return None
    try:
        return json.loads(TOKENS_PATH.read_text())
    except (OSError, ValueError):
        return None


def _save_tokens(data: dict) -> None:
    TOKENS_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOKENS_PATH.write_text(json.dumps(data, indent=2))


# ---------------------------------------------------------------------------
# MSA v2 OAuth (Azure app registration)
# ---------------------------------------------------------------------------
def _authorization_url(redirect_uri: str, state: str) -> str:
    params = {
        "client_id": CLIENT_ID,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": SCOPES,
        "response_mode": "query",
        "state": state,
        "prompt": "select_account",
    }
    return f"{_AUTHORIZE_URL}?{urlencode(params)}"


async def _exchange_code(
    client: httpx.AsyncClient, code: str, redirect_uri: str
) -> dict:
    resp = await client.post(
        _TOKEN_URL,
        data={
            "client_id": CLIENT_ID,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "scope": SCOPES,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


async def _refresh_msa(client: httpx.AsyncClient, refresh_token: str) -> dict:
    resp = await client.post(
        _TOKEN_URL,
        data={
            "client_id": CLIENT_ID,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "scope": SCOPES,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Xbox Live user + XSTS token exchange
# ---------------------------------------------------------------------------
async def _xbl_user_token(
    client: httpx.AsyncClient, msa_access_token: str
) -> dict:
    resp = await client.post(
        _XBL_USER_URL,
        json={
            "RelyingParty": "http://auth.xboxlive.com",
            "TokenType": "JWT",
            "Properties": {
                "AuthMethod": "RPS",
                "SiteName": "user.auth.xboxlive.com",
                # "d=" prefix marks this as an MSA v2 access token; the
                # legacy well-known-clientid flow used "t=" for compact
                # tickets. Xbox Live's user auth service accepts both.
                "RpsTicket": f"d={msa_access_token}",
            },
        },
        headers={
            "x-xbl-contract-version": "1",
            "Content-Type": "application/json",
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


async def _xsts_token(client: httpx.AsyncClient, user_token: str) -> dict:
    resp = await client.post(
        _XSTS_URL,
        json={
            "RelyingParty": "http://xboxlive.com",
            "TokenType": "JWT",
            "Properties": {
                "SandboxId": "RETAIL",
                "UserTokens": [user_token],
            },
        },
        headers={
            "x-xbl-contract-version": "1",
            "Content-Type": "application/json",
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


async def _get_xsts(client: httpx.AsyncClient) -> Optional[tuple[str, str]]:
    """Return ``(auth_header, xuid)`` for authenticated Xbox Live calls.

    Refreshes MSA tokens on-disk if needed. Returns ``None`` if the chain
    is broken so callers can degrade to "no playtime".
    """
    tokens = _load_tokens()
    if not tokens or "refresh_token" not in tokens:
        return None
    try:
        msa = await _refresh_msa(client, tokens["refresh_token"])
    except httpx.HTTPError as exc:
        log.warning("MSA refresh failed: %s", exc)
        return None
    # Persist the rotated refresh token immediately — MS invalidates the old
    # one, so a crash mid-chain would otherwise strand us.
    tokens["access_token"] = msa["access_token"]
    tokens["refresh_token"] = msa.get("refresh_token", tokens["refresh_token"])
    tokens["expires_at"] = int(time.time()) + int(msa.get("expires_in", 3600))
    _save_tokens(tokens)

    try:
        user = await _xbl_user_token(client, msa["access_token"])
        xsts = await _xsts_token(client, user["Token"])
    except httpx.HTTPError as exc:
        log.warning("XBL/XSTS exchange failed: %s", exc)
        return None

    claims = xsts["DisplayClaims"]["xui"][0]
    return (
        f"XBL3.0 x={claims['uhs']};{xsts['Token']}",
        claims["xid"],
    )


# ---------------------------------------------------------------------------
# userstats.xboxlive.com/batch
# ---------------------------------------------------------------------------
async def _fetch_one(
    client: httpx.AsyncClient,
    auth_header: str,
    xuid: str,
    title_id: str,
    scid: str,
) -> Optional[int]:
    body = {
        "arrangebyfield": "xuid",
        "xuids": [xuid],
        "stats": [{"name": "MinutesPlayed", "scid": scid}],
    }
    headers = {
        "Authorization": auth_header,
        "x-xbl-contract-version": "2",
        "Accept": "application/json",
        "Accept-Language": "en-US",
        "Content-Type": "application/json",
    }
    try:
        resp = await client.post(
            "https://userstats.xboxlive.com/batch",
            json=body,
            headers=headers,
            timeout=15,
        )
    except (httpx.HTTPError, OSError) as exc:
        log.debug("stats fetch failed for %s: %s", title_id, exc)
        return None
    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
        for stat_list in data.get("statlistscollection", []):
            for stat in stat_list.get("stats", []):
                if stat.get("name") != "MinutesPlayed":
                    continue
                value = stat.get("value")
                if value in (None, ""):
                    continue
                return int(value)
    except (ValueError, KeyError, TypeError) as exc:
        log.debug("could not parse stats for %s: %s", title_id, exc)
    return None


async def _fetch_all_async(
    pairs: Iterable[tuple[str, str]],
) -> dict[str, int]:
    async with httpx.AsyncClient() as client:
        ctx = await _get_xsts(client)
        if ctx is None:
            return {}
        auth_header, xuid = ctx
        out: dict[str, int] = {}
        for title_id, scid in pairs:
            minutes = await _fetch_one(client, auth_header, xuid, title_id, scid)
            if minutes is not None:
                out[title_id] = minutes
        return out


def fetch_playtime_minutes(title_ids: Iterable[str]) -> dict[str, int]:
    """Best-effort ``{title_id: MinutesPlayed}`` map for the given titles.

    Returns ``{}`` if the auth stack isn't set up, the Xbox app cache has no
    SCID for any of the requested titles, or the network is unavailable.
    Never raises — every failure mode degrades to a smaller result map so
    ``sync_xbox()`` continues without interruption.
    """
    title_ids = list(title_ids)
    if not title_ids or not TOKENS_PATH.exists():
        return {}
    scid_map = _load_scid_map()
    if not scid_map:
        return {}
    pairs = [(tid, scid_map[tid]) for tid in title_ids if tid in scid_map]
    if not pairs:
        return {}
    try:
        return asyncio.run(_fetch_all_async(pairs))
    except RuntimeError as exc:
        if "cannot be called" not in str(exc):
            log.warning("Xbox playtime fetch failed: %s", exc)
            return {}
        # Nested event loop (e.g. Flask debug reloader). Use a fresh loop.
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(_fetch_all_async(pairs))
        finally:
            loop.close()


# ---------------------------------------------------------------------------
# One-time interactive login CLI:  python -m gigalib.xbox_stats login
# ---------------------------------------------------------------------------
def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _run_loopback_server(port: int, state: str, timeout: float) -> dict:
    """Block until MS redirects back with ?code=... or ?error=..., then stop.

    Returns the parsed query string (may include ``code`` and/or ``error``).
    Raises ``TimeoutError`` if nothing arrives inside ``timeout`` seconds.
    """
    captured: dict[str, str] = {}
    done = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - stdlib API
            parsed = urlparse(self.path)
            qs = {k: v[0] for k, v in parse_qs(parsed.query).items()}
            # Ignore browser prefetches / favicon probes.
            if "code" not in qs and "error" not in qs:
                self.send_response(404)
                self.end_headers()
                return
            if qs.get("state") != state:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"state mismatch")
                return
            captured.update(qs)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            if "code" in qs:
                self.wfile.write(
                    b"<!doctype html><html><body style='font-family:sans-serif;"
                    b"text-align:center;padding-top:4rem'>"
                    b"<h1>Signed in to GigaLib</h1>"
                    b"<p>You can close this tab and return to the terminal.</p>"
                    b"</body></html>"
                )
            else:
                msg = qs.get("error_description", qs.get("error", "unknown"))
                self.wfile.write(
                    f"<!doctype html><html><body style='font-family:sans-serif;"
                    f"padding:2rem'><h1>Sign-in failed</h1><pre>{msg}</pre>"
                    f"</body></html>".encode()
                )
            done.set()

        def log_message(self, *_args, **_kwargs):
            # Silence the default stderr access log.
            pass

    server = HTTPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        if not done.wait(timeout=timeout):
            raise TimeoutError(
                f"no OAuth callback within {int(timeout)}s"
            )
    finally:
        server.shutdown()
        thread.join(timeout=2)
    return captured


async def _login_async() -> None:
    port = _free_port()
    # MSA's consumer endpoint requires the redirect URI to *exactly* match a
    # registered URI. Registering ``http://localhost`` grants us any port,
    # but we must not add a path component (``/callback`` breaks the match).
    redirect_uri = f"http://localhost:{port}/"
    # random state to defend against cross-site redirect injection.
    state = os.urandom(16).hex()
    url = _authorization_url(redirect_uri, state)

    print("Opening your browser to sign in to Xbox Live...")
    print(f"If it doesn't open automatically, visit:\n  {url}")
    try:
        webbrowser.open(url)
    except webbrowser.Error:
        pass  # user will open manually

    print("Waiting for sign-in to complete (5 minute timeout)...")
    try:
        qs = await asyncio.get_running_loop().run_in_executor(
            None, _run_loopback_server, port, state, 300.0
        )
    except TimeoutError as exc:
        print(f"login timed out: {exc}", file=sys.stderr)
        return

    if "error" in qs:
        print(
            f"login failed: {qs.get('error')}: {qs.get('error_description', '')}",
            file=sys.stderr,
        )
        return

    code = qs["code"]
    async with httpx.AsyncClient() as client:
        try:
            msa = await _exchange_code(client, code, redirect_uri)
        except httpx.HTTPError as exc:
            print(f"token exchange failed: {exc}", file=sys.stderr)
            body = getattr(exc, "response", None)
            if body is not None:
                print(body.text, file=sys.stderr)
            return
        tokens = {
            "access_token": msa["access_token"],
            "refresh_token": msa.get("refresh_token", ""),
            "expires_at": int(time.time()) + int(msa.get("expires_in", 3600)),
        }
        _save_tokens(tokens)
        # Prove the full chain works before declaring success.
        ctx = await _get_xsts(client)
    if ctx is None:
        print(
            "MSA login succeeded but XBL/XSTS handshake failed.",
            file=sys.stderr,
        )
        return
    _auth_header, xuid = ctx
    print(f"saved tokens to {TOKENS_PATH.resolve()}")
    print(f"authenticated XUID: {xuid}")


def _main() -> None:
    if sys.argv[1:] == ["login"]:
        asyncio.run(_login_async())
        return
    print("usage: python -m gigalib.xbox_stats login", file=sys.stderr)
    sys.exit(2)


if __name__ == "__main__":
    _main()
