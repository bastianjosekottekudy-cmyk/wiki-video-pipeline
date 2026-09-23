"""YouTube OAuth authentication (multi-client failover chain)."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import secrets
import subprocess
import sys
import threading
import time
import webbrowser
import wsgiref.simple_server
from base64 import urlsafe_b64encode
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import ConnectTimeout, Timeout

from src.config import PROJECT_ROOT, SECRETS_DIR, get_env, load_pipeline_config

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
# Backward-compatible aliases for primary client paths.
TOKEN_PATH = SECRETS_DIR / "token.json"
DEFAULT_WEB_REDIRECT_URI = "http://127.0.0.1:8082/api/youtube/oauth/callback"


@dataclass(frozen=True)
class YouTubeClient:
    id: str
    client_secrets: Path
    token: Path


def _resolve_path(raw: str | Path) -> Path:
    path = Path(raw)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path


def _default_primary_client() -> YouTubeClient:
    secrets_path = Path(
        get_env("YOUTUBE_CLIENT_SECRETS", str(SECRETS_DIR / "client_secrets.json"))
    )
    if not secrets_path.is_absolute():
        secrets_path = PROJECT_ROOT / secrets_path
    return YouTubeClient(
        id="primary",
        client_secrets=secrets_path,
        token=TOKEN_PATH,
    )


def list_youtube_clients() -> list[YouTubeClient]:
    """Ordered OAuth clients from pipeline.yaml (or single primary fallback)."""
    cfg = load_pipeline_config().get("youtube") or {}
    raw_clients = cfg.get("clients")
    if not isinstance(raw_clients, list) or not raw_clients:
        return [_default_primary_client()]

    clients: list[YouTubeClient] = []
    for entry in raw_clients:
        if not isinstance(entry, dict):
            continue
        cid = str(entry.get("id") or "").strip()
        secrets_raw = entry.get("client_secrets")
        token_raw = entry.get("token")
        if not cid or not secrets_raw or not token_raw:
            logger.warning("Skipping incomplete youtube.clients entry: %s", entry)
            continue
        clients.append(
            YouTubeClient(
                id=cid,
                client_secrets=_resolve_path(str(secrets_raw)),
                token=_resolve_path(str(token_raw)),
            )
        )
    return clients or [_default_primary_client()]


def get_youtube_client(client_id: str) -> YouTubeClient:
    wanted = (client_id or "primary").strip()
    for client in list_youtube_clients():
        if client.id == wanted:
            return client
    known = ", ".join(c.id for c in list_youtube_clients()) or "(none)"
    raise ValueError(f"Unknown YouTube client {wanted!r}. Known: {known}")


def _is_invalid_grant(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "invalid_grant" in text or "expired or revoked" in text


def _sync_token_to_central_store(client_id: str, token_path: Path) -> None:
    try:
        candidates = [
            Path.home()
            / ".agents"
            / "skills"
            / "google-auth"
            / "secrets"
            / "tokens"
            / "youtube"
            / client_id
            / "token.json",
            Path.home()
            / ".cursor"
            / "skills"
            / "google-auth"
            / "secrets"
            / "tokens"
            / "youtube"
            / client_id
            / "token.json",
        ]
        for c in candidates:
            if c.parent.parent.parent.exists():
                c.parent.mkdir(parents=True, exist_ok=True)
                c.write_text(token_path.read_text(encoding="utf-8"), encoding="utf-8")
                logger.info("Backed up %s token to central store %s", client_id, c)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not sync token to central store: %s", exc)


def _save_token(creds: Credentials, token_path: Path, client_id: str = "") -> None:
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    if client_id:
        _sync_token_to_central_store(client_id, token_path)


def _clear_stale_token(token_path: Path) -> None:
    if token_path.exists():
        token_path.unlink()
        logger.warning(
            "Removed stale YouTube token at %s — re-auth required", token_path
        )


def _refresh_or_raise(creds: Credentials, token_path: Path, client_id: str = "") -> Credentials:
    creds.refresh(Request())
    _save_token(creds, token_path, client_id=client_id)
    return creds


def _status_dict(
    client: YouTubeClient,
    status: str,
    *,
    detail: str = "",
    can_refresh: bool | None = None,
) -> dict[str, Any]:
    if can_refresh is None:
        can_refresh = client.client_secrets.is_file()
    return {
        "id": client.id,
        "status": status,
        "detail": detail[:240],
        "can_refresh": can_refresh,
        "has_secrets": client.client_secrets.is_file(),
        "has_token": client.token.is_file(),
        "action_label": "Refresh" if (status == "ok" or can_refresh) else "Authorize",
    }


def probe_client_status(
    client: YouTubeClient,
    *,
    attempt_refresh: bool = True,
) -> dict[str, Any]:
    """Inspect one client's token health without opening a browser."""
    if not client.client_secrets.is_file():
        return _status_dict(
            client,
            "missing_secrets",
            detail="client_secrets.json not found",
            can_refresh=False,
        )
    if not client.token.is_file():
        return _status_dict(
            client,
            "missing_token",
            detail="No token — authorize to enable uploads",
            can_refresh=False,
        )

    try:
        creds = Credentials.from_authorized_user_file(str(client.token), SCOPES)
    except Exception as exc:  # noqa: BLE001
        return _status_dict(
            client,
            "needs_reauth",
            detail=f"Unreadable token ({exc})",
            can_refresh=False,
        )

    if creds and creds.valid:
        return _status_dict(client, "ok", detail="Token valid", can_refresh=True)

    if creds and creds.refresh_token:
        if not attempt_refresh:
            return _status_dict(
                client,
                "ok",
                detail="Connected (Auto-refresh on demand)",
                can_refresh=True,
            )
        try:
            _refresh_or_raise(creds, client.token, client_id=client.id)
            return _status_dict(
                client, "ok", detail="Token refreshed & valid", can_refresh=True
            )
        except RefreshError as exc:
            if _is_invalid_grant(exc):
                _clear_stale_token(client.token)
                return _status_dict(
                    client,
                    "needs_reauth",
                    detail="Refresh token revoked — re-authorize",
                    can_refresh=True,
                )
            return _status_dict(
                client,
                "error",
                detail=f"Refresh failed: {exc}",
                can_refresh=True,
            )
        except Exception as exc:  # noqa: BLE001
            return _status_dict(
                client,
                "error",
                detail=f"Refresh failed: {exc}",
                can_refresh=True,
            )

    return _status_dict(
        client,
        "needs_reauth",
        detail="No usable refresh token — re-authorize",
        can_refresh=True,
    )


def probe_youtube_clients(*, attempt_refresh: bool = True) -> list[dict[str, Any]]:
    return [
        probe_client_status(c, attempt_refresh=attempt_refresh)
        for c in list_youtube_clients()
    ]


class OAuthAuthSession:
    def __init__(self, client: YouTubeClient, timeout: float = 600.0) -> None:
        self.client = client
        self.timeout = timeout
        self.created_at = time.time()
        self.flow: InstalledAppFlow | None = None
        self.server: wsgiref.simple_server.WSGIServer | None = None
        self.server_port: int = 0
        self.thread: threading.Thread | None = None
        self.auth_url: str = ""
        self.status: str = "pending"  # pending, completed, error, timed_out
        self.ok: bool = False
        self.detail: str = "Waiting for user sign-in"
        self.received_query: str = ""
        self._stop = False
        self._completed_event = threading.Event()

    def is_alive(self) -> bool:
        if self.status != "pending":
            return False
        if time.time() - self.created_at > self.timeout:
            self.status = "timed_out"
            self.detail = "Authorization timed out. Please try again."
            self.close()
            return False
        return True

    def submit_callback(self, raw_input: str) -> dict[str, Any]:
        raw = raw_input.strip()
        if not raw:
            return {"ok": False, "detail": "Empty authorization response"}
        if "?" in raw:
            query = raw.partition("?")[2]
        elif "&" in raw or "=" in raw:
            query = raw
        else:
            query = f"code={raw}"

        if "error=" in query:
            self.status = "error"
            self.detail = "Authorization was denied by Google account."
            return {"ok": False, "detail": self.detail}

        self.received_query = query

        if self.thread and self.thread.is_alive():
            if self._completed_event.wait(timeout=10.0):
                return self.to_dict()

        try:
            port = self.server_port or (self.server.server_port if self.server else 0)
            authorization_response = f"https://localhost:{port}/?{self.received_query}"
            last_exc: BaseException | None = None
            for attempt in range(1, 6):
                try:
                    self.flow.fetch_token(authorization_response=authorization_response)
                    break
                except Exception as exc:
                    last_exc = exc
                    time.sleep(min(attempt * 1.5, 6))
            else:
                raise last_exc or RuntimeError("Failed to exchange OAuth token")

            creds = self.flow.credentials
            _save_token(creds, self.client.token, client_id=self.client.id)
            self.status = "completed"
            self.ok = True
            self.detail = "Authorized successfully"
            logger.info("Successfully authorized YouTube client %s via callback submission", self.client.id)
            self._completed_event.set()
            self.close()
            return self.to_dict()
        except Exception as exc:
            logger.exception("Callback token exchange failed for %s: %s", self.client.id, exc)
            self.status = "error"
            self.detail = str(exc)[:240]
            return self.to_dict()

    def start(self) -> str:
        flow = InstalledAppFlow.from_client_secrets_file(str(self.client.client_secrets), SCOPES)
        self.flow = flow

        session_ref = self

        class _CallbackApp:
            def __call__(self, environ: dict[str, Any], start_response: Any) -> list[bytes]:
                query = environ.get("QUERY_STRING", "")
                if "code=" in query or "error=" in query:
                    session_ref.received_query = query
                    start_response("200 OK", [("Content-type", "text/html; charset=utf-8")])
                    html = (
                        "<!DOCTYPE html>"
                        "<html>"
                        "<head><meta charset='utf-8'><title>YouTube Authorization Complete</title></head>"
                        "<body style='font-family:system-ui,-apple-system,sans-serif;text-align:center;padding:60px;background:#18181b;color:#f4f4f5;'>"
                        "<div style='max-width:480px;margin:0 auto;background:#27272a;padding:32px;border-radius:12px;box-shadow:0 4px 20px rgba(0,0,0,0.5);'>"
                        "<h2 style='color:#22c55e;margin-top:0;'>✓ Authorization Successful</h2>"
                        "<p style='color:#a1a1aa;line-height:1.6;'>YouTube OAuth credentials have been saved. You can close this window and return to the dashboard.</p>"
                        "<script>setTimeout(function(){ window.close(); }, 2500);</script>"
                        "</div>"
                        "</body>"
                        "</html>"
                    )
                    return [html.encode("utf-8")]
                else:
                    start_response("404 Not Found", [("Content-type", "text/plain")])
                    return [b"Not found"]

        class _QuietHandler(wsgiref.simple_server.WSGIRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                pass

        wsgiref.simple_server.WSGIServer.allow_reuse_address = False
        local_server = wsgiref.simple_server.make_server(
            "localhost", 0, _CallbackApp(), handler_class=_QuietHandler
        )
        local_server.timeout = 1.5
        self.server = local_server
        self.server_port = local_server.server_port

        flow.redirect_uri = f"http://localhost:{local_server.server_port}/"
        auth_url, _ = flow.authorization_url(access_type="offline", prompt="consent")
        self.auth_url = auth_url

        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        return auth_url

    def _run(self) -> None:
        deadline = self.created_at + self.timeout
        try:
            while time.time() < deadline and not self._stop:
                if self.server is not None:
                    self.server.handle_request()
                if self.received_query:
                    break

            if not self.received_query:
                self.status = "timed_out"
                self.detail = "Authorization timed out. Please try again."
                return

            if "error=" in self.received_query:
                self.status = "error"
                self.detail = "Authorization was denied by Google account."
                return

            port = self.server_port or (self.server.server_port if self.server else 0)
            authorization_response = f"https://localhost:{port}/?{self.received_query}"
            last_exc: BaseException | None = None
            for attempt in range(1, 6):
                try:
                    self.flow.fetch_token(authorization_response=authorization_response)
                    break
                except Exception as exc:
                    last_exc = exc
                    time.sleep(min(attempt * 2, 8))
            else:
                raise last_exc or RuntimeError("Failed to exchange OAuth token")

            creds = self.flow.credentials
            _save_token(creds, self.client.token, client_id=self.client.id)
            self.status = "completed"
            self.ok = True
            self.detail = "Authorized successfully"
            logger.info("Successfully authorized YouTube client %s", self.client.id)
        except Exception as exc:
            logger.exception("Interactive OAuth flow failed for %s: %s", self.client.id, exc)
            self.status = "error"
            self.detail = str(exc)[:240]
        finally:
            self._completed_event.set()
            self.close()

    def wait_completion(self, timeout: float = 600.0) -> bool:
        return self._completed_event.wait(timeout=timeout)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.client.id,
            "status": self.status,
            "ok": self.ok,
            "detail": self.detail,
            "auth_url": self.auth_url,
        }

    def close(self) -> None:
        self._stop = True
        if self.server is not None:
            try:
                self.server.server_close()
            except Exception:
                pass
            self.server = None


_active_sessions: dict[str, OAuthAuthSession] = {}
_session_lock = threading.Lock()


def start_auth_session(client_id: str, timeout: float = 600.0) -> dict[str, Any]:
    client = get_youtube_client(client_id)
    if not client.client_secrets.is_file():
        raise FileNotFoundError(
            f"YouTube client secrets not found for {client.id} at {client.client_secrets}"
        )

    with _session_lock:
        existing = _active_sessions.get(client.id)
        if existing and existing.is_alive():
            return existing.to_dict()
        elif existing:
            existing.close()

        session = OAuthAuthSession(client, timeout=timeout)
        session.start()
        _active_sessions[client.id] = session
        return session.to_dict()


def submit_auth_callback(client_id: str, raw_input: str) -> dict[str, Any]:
    with _session_lock:
        session = _active_sessions.get(client_id)
    if not session:
        return {
            "id": client_id,
            "status": "error",
            "ok": False,
            "detail": "No active authorization session found. Please click Authorize first.",
        }
    return session.submit_callback(raw_input)


def get_auth_session_status(client_id: str) -> dict[str, Any]:
    client = get_youtube_client(client_id)
    with _session_lock:
        session = _active_sessions.get(client.id)
        if session:
            session.is_alive()
            return session.to_dict()

    probe = probe_client_status(client, attempt_refresh=False)
    return {
        "id": client.id,
        "status": "completed" if probe.get("status") == "ok" else "idle",
        "ok": probe.get("status") == "ok",
        "detail": probe.get("detail", ""),
        "auth_url": "",
    }


def try_silent_refresh(client_id: str) -> dict[str, Any]:
    """
    Attempt silent token refresh for one client.
    Returns status dict plus ok / needs_browser flags and auth_url if needed.
    """
    client = get_youtube_client(client_id)
    if not client.client_secrets.is_file():
        result = _status_dict(
            client,
            "missing_secrets",
            detail="client_secrets.json not found",
            can_refresh=False,
        )
        result["ok"] = False
        result["needs_browser"] = False
        return result

    if not client.token.is_file():
        result = _status_dict(
            client,
            "missing_token",
            detail="No token — browser authorization required",
            can_refresh=True,
        )
        result["ok"] = False
        result["needs_browser"] = True
        session = start_auth_session(client.id)
        result["auth_url"] = session.get("auth_url", "")
        return result

    try:
        creds = Credentials.from_authorized_user_file(str(client.token), SCOPES)
    except Exception as exc:  # noqa: BLE001
        result = _status_dict(
            client,
            "needs_reauth",
            detail=f"Unreadable token ({exc})",
            can_refresh=True,
        )
        result["ok"] = False
        result["needs_browser"] = True
        session = start_auth_session(client.id)
        result["auth_url"] = session.get("auth_url", "")
        return result

    if creds and creds.valid:
        result = _status_dict(
            client, "ok", detail="Token already valid", can_refresh=True
        )
        result["ok"] = True
        result["needs_browser"] = False
        return result

    if not (creds and creds.refresh_token):
        result = _status_dict(
            client,
            "needs_reauth",
            detail="No refresh token — browser authorization required",
            can_refresh=True,
        )
        result["ok"] = False
        result["needs_browser"] = True
        session = start_auth_session(client.id)
        result["auth_url"] = session.get("auth_url", "")
        return result

    try:
        _refresh_or_raise(creds, client.token, client_id=client.id)
        result = _status_dict(client, "ok", detail="Token refreshed", can_refresh=True)
        result["ok"] = True
        result["needs_browser"] = False
        return result
    except RefreshError as exc:
        if _is_invalid_grant(exc):
            _clear_stale_token(client.token)
            result = _status_dict(
                client,
                "needs_reauth",
                detail="Refresh token revoked — re-authorize in browser",
                can_refresh=True,
            )
            result["ok"] = False
            result["needs_browser"] = True
            session = start_auth_session(client.id)
            result["auth_url"] = session.get("auth_url", "")
            return result
        result = _status_dict(
            client,
            "needs_reauth",
            detail=f"Refresh failed: {exc}",
            can_refresh=True,
        )
        result["ok"] = False
        result["needs_browser"] = True
        session = start_auth_session(client.id)
        result["auth_url"] = session.get("auth_url", "")
        return result
    except Exception as exc:  # noqa: BLE001
        result = _status_dict(
            client,
            "error",
            detail=f"Refresh failed: {exc}",
            can_refresh=True,
        )
        result["ok"] = False
        result["needs_browser"] = True
        session = start_auth_session(client.id)
        result["auth_url"] = session.get("auth_url", "")
        return result


def authorize_client_interactive(client_id: str, timeout: float = 180.0) -> dict[str, Any]:
    """
    Open Google login via Desktop loopback and save the token.
    Compatible with CLI and direct calls.
    """
    session_info = start_auth_session(client_id, timeout=timeout)
    auth_url = session_info.get("auth_url", "")
    logger.info("Please visit this URL to authorize this application: %s", auth_url)
    print(f"Please visit this URL to authorize this application: {auth_url}", flush=True)
    _open_auth_browser(auth_url)

    with _session_lock:
        session = _active_sessions.get(client_id)
    if session:
        session.wait_completion(timeout=timeout)
        return session.to_dict()
    return probe_client_status(get_youtube_client(client_id), attempt_refresh=False)


def _open_auth_browser(url: str) -> None:
    """Best-effort open of the system browser (uvicorn threads can be flaky)."""
    opened = False
    try:
        opened = bool(webbrowser.open(url, new=1, autoraise=True))
    except Exception as exc:  # noqa: BLE001
        logger.warning("webbrowser.open failed: %s", exc)
    if sys.platform == "win32":
        try:
            subprocess.Popen(
                ["cmd", "/c", "start", "", url],
                close_fds=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            opened = True
        except Exception as exc:  # noqa: BLE001
            logger.warning("cmd start browser failed: %s", exc)
    if not opened:
        logger.warning("Could not auto-open browser — use the auth URL from the logs")


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


def _load_client_config(client: YouTubeClient) -> dict[str, Any]:
    with client.client_secrets.open(encoding="utf-8") as f:
        raw = json.load(f)
    installed = raw.get("installed") or raw.get("web")
    if not isinstance(installed, dict):
        raise ValueError(f"Invalid client secrets for {client.id}")
    return installed


def build_web_oauth_authorization(
    client_id: str,
    *,
    redirect_uri: str = DEFAULT_WEB_REDIRECT_URI,
) -> dict[str, str]:
    """Start web OAuth; returns auth_url, state, code_verifier."""
    client = get_youtube_client(client_id)
    if not client.client_secrets.is_file():
        raise FileNotFoundError(
            f"YouTube client secrets not found for {client.id} at {client.client_secrets}"
        )
    cfg = _load_client_config(client)
    state = secrets.token_urlsafe(24)
    code_verifier, code_challenge = _pkce_pair()
    params = {
        "client_id": cfg["client_id"],
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    auth_uri = cfg.get("auth_uri", "https://accounts.google.com/o/oauth2/auth")
    auth_url = f"{auth_uri}?{urlencode(params)}"
    return {
        "auth_url": auth_url,
        "state": state,
        "code_verifier": code_verifier,
        "client_id": client.id,
        "redirect_uri": redirect_uri,
    }


def exchange_web_oauth_code(
    client_id: str,
    *,
    code: str,
    code_verifier: str,
    redirect_uri: str = DEFAULT_WEB_REDIRECT_URI,
) -> Credentials:
    """Exchange authorization code for tokens; save to the client token path."""
    client = get_youtube_client(client_id)
    flow = InstalledAppFlow.from_client_secrets_file(str(client.client_secrets), SCOPES)
    flow.redirect_uri = redirect_uri
    flow.code_verifier = code_verifier

    last_exc: BaseException | None = None
    for attempt in range(1, 8):
        try:
            flow.fetch_token(code=code)
            break
        except (ConnectTimeout, Timeout, RequestsConnectionError, OSError) as exc:
            last_exc = exc
            wait = min(2 * attempt, 12)
            logger.warning(
                "Web token exchange failed (attempt %s/7): %s — retry in %ss",
                attempt,
                exc,
                wait,
            )
            time.sleep(wait)
    else:
        assert last_exc is not None
        raise last_exc

    creds = flow.credentials
    _save_token(creds, client.token)
    logger.info("Web OAuth complete for %s. Token saved to %s", client.id, client.token)
    return creds


def _run_browser_oauth(client_path: Path) -> Credentials:
    """
    Browser OAuth with retries on token exchange.
    Sign-in happens once; flaky oauth2.googleapis.com connectivity is retried.
    """
    from google_auth_oauthlib.flow import _RedirectWSGIApp, _WSGIRequestHandler

    flow = InstalledAppFlow.from_client_secrets_file(str(client_path), SCOPES)
    wsgi_app = _RedirectWSGIApp(
        "Authentication complete. You can close this window."
    )
    wsgiref.simple_server.WSGIServer.allow_reuse_address = False
    local_server = wsgiref.simple_server.make_server(
        "localhost", 0, wsgi_app, handler_class=_WSGIRequestHandler
    )
    try:
        flow.redirect_uri = f"http://localhost:{local_server.server_port}/"
        auth_url, _ = flow.authorization_url(
            access_type="offline", prompt="consent"
        )
        logger.info("Please visit this URL to authorize this application: %s", auth_url)
        print(f"Please visit this URL to authorize this application: {auth_url}", flush=True)
        _open_auth_browser(auth_url)
        local_server.handle_request()
        if not wsgi_app.last_request_uri:
            raise RuntimeError("Timed out waiting for OAuth browser callback")
        authorization_response = wsgi_app.last_request_uri.replace("http", "https")

        last_exc: BaseException | None = None
        for attempt in range(1, 8):
            try:
                flow.fetch_token(authorization_response=authorization_response)
                break
            except (ConnectTimeout, Timeout, RequestsConnectionError, OSError) as exc:
                last_exc = exc
                wait = min(2 * attempt, 12)
                logger.warning(
                    "Token exchange failed (attempt %s/7): %s — retry in %ss",
                    attempt,
                    exc,
                    wait,
                )
                time.sleep(wait)
        else:
            assert last_exc is not None
            raise last_exc
    finally:
        local_server.server_close()

    return flow.credentials


def get_credentials_for_client(
    client: YouTubeClient,
    *,
    allow_browser: bool = True,
) -> Credentials:
    """
    Load/refresh OAuth credentials for one client.
    Env YOUTUBE_REFRESH_TOKEN is only used for the primary client.
    """
    client_path = client.client_secrets
    token_path = client.token
    refresh_token = ""
    if client.id == "primary":
        refresh_token = get_env("YOUTUBE_REFRESH_TOKEN", "").strip()

    creds: Credentials | None = None

    if token_path.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Could not load token for %s (%s) — will re-auth", client.id, exc
            )
            _clear_stale_token(token_path)
            creds = None

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            return _refresh_or_raise(creds, token_path)
        except RefreshError as exc:
            if not _is_invalid_grant(exc):
                raise
            logger.warning(
                "YouTube token refresh failed for %s (invalid_grant): %s",
                client.id,
                exc,
            )
            _clear_stale_token(token_path)
            creds = None

    if refresh_token and client_path.exists():
        try:
            with client_path.open(encoding="utf-8") as f:
                client_config = json.load(f)
            installed = client_config.get("installed") or client_config.get("web", {})
            creds = Credentials(
                token=None,
                refresh_token=refresh_token,
                token_uri=installed.get(
                    "token_uri", "https://oauth2.googleapis.com/token"
                ),
                client_id=installed["client_id"],
                client_secret=installed["client_secret"],
                scopes=SCOPES,
            )
            return _refresh_or_raise(creds, token_path)
        except RefreshError as exc:
            if not _is_invalid_grant(exc):
                raise
            logger.warning(
                "YOUTUBE_REFRESH_TOKEN is expired/revoked for %s — "
                "starting browser OAuth",
                client.id,
            )
            creds = None

    if not client_path.exists():
        raise FileNotFoundError(
            f"YouTube client secrets not found for {client.id} at {client_path}. "
            "Download OAuth credentials from Google Cloud Console."
        )

    if not allow_browser:
        raise RuntimeError(
            f"YouTube client {client.id} needs interactive OAuth. "
            f"Run: python -m src.youtube.auth --client {client.id}"
        )

    creds = _run_browser_oauth(client_path)
    _save_token(creds, token_path)
    logger.info("OAuth complete for %s. Token saved to %s", client.id, token_path)
    if client.id == "primary" and creds.refresh_token:
        logger.info(
            "Add this to .env as YOUTUBE_REFRESH_TOKEN=%s", creds.refresh_token
        )
    return creds


def get_credentials() -> Credentials:
    """Backward-compatible: credentials for the first configured client."""
    return get_credentials_for_client(list_youtube_clients()[0])


def main() -> None:
    """Run OAuth flow: python -m src.youtube.auth [--client primary|backup1|...]"""
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description="Authorize a YouTube OAuth client")
    parser.add_argument(
        "--client",
        default="primary",
        help="Client id from youtube.clients in pipeline.yaml (default: primary)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Ignore existing valid token and open browser login",
    )
    args = parser.parse_args()

    SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    client = get_youtube_client(args.client)
    client.token.parent.mkdir(parents=True, exist_ok=True)

    if args.force:
        result = authorize_client_interactive(client.id)
        if not result.get("ok"):
            raise SystemExit(result.get("detail") or "Authorization failed")
        print(f"Authenticated successfully ({client.id}).")
        print(f"Token saved to: {client.token}")
        return

    if client.token.exists():
        try:
            probe = Credentials.from_authorized_user_file(str(client.token), SCOPES)
            if probe and probe.expired and probe.refresh_token:
                probe.refresh(Request())
                _save_token(probe, client.token)
                print(f"Existing token for {client.id} refreshed successfully.")
                print(f"Token saved to: {client.token}")
                return
            if probe and probe.valid:
                print(f"Existing token for {client.id} is still valid.")
                print(f"Token saved to: {client.token}")
                return
        except RefreshError as exc:
            if _is_invalid_grant(exc):
                logger.warning(
                    "Stored token for %s invalid — clearing and opening browser login",
                    client.id,
                )
                _clear_stale_token(client.token)
            else:
                raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Could not use existing token for %s (%s) — re-auth", client.id, exc
            )
            _clear_stale_token(client.token)

    # Missing/invalid token: prefer interactive browser (skips revoked env refresh).
    result = authorize_client_interactive(client.id)
    if not result.get("ok"):
        raise SystemExit(result.get("detail") or "Authorization failed")
    print(f"Authenticated successfully ({client.id}).")
    print(f"Token saved to: {client.token}")


if __name__ == "__main__":
    main()
