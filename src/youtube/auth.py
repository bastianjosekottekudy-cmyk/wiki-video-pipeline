"""YouTube OAuth authentication (multi-client failover chain)."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import secrets
import subprocess
import sys
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


def _save_token(creds: Credentials, token_path: Path) -> None:
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json(), encoding="utf-8")


def _clear_stale_token(token_path: Path) -> None:
    if token_path.exists():
        token_path.unlink()
        logger.warning(
            "Removed stale YouTube token at %s — re-auth required", token_path
        )


def _refresh_or_raise(creds: Credentials, token_path: Path) -> Credentials:
    creds.refresh(Request())
    _save_token(creds, token_path)
    return creds


def _status_dict(
    client: YouTubeClient,
    status: str,
    *,
    detail: str = "",
    can_refresh: bool | None = None,
) -> dict[str, Any]:
    if can_refresh is None:
        can_refresh = (
            status not in ("ok", "missing_secrets") and client.client_secrets.is_file()
        )
    return {
        "id": client.id,
        "status": status,
        "detail": detail[:240],
        "can_refresh": can_refresh,
        "has_secrets": client.client_secrets.is_file(),
        "has_token": client.token.is_file(),
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
            can_refresh=True,
        )

    try:
        creds = Credentials.from_authorized_user_file(str(client.token), SCOPES)
    except Exception as exc:  # noqa: BLE001
        return _status_dict(
            client,
            "needs_reauth",
            detail=f"Unreadable token ({exc})",
            can_refresh=True,
        )

    if creds and creds.valid:
        return _status_dict(client, "ok", detail="Token valid", can_refresh=False)

    if creds and creds.expired and creds.refresh_token:
        if not attempt_refresh:
            return _status_dict(
                client,
                "expired",
                detail="Access token expired — refresh available",
                can_refresh=True,
            )
        try:
            _refresh_or_raise(creds, client.token)
            return _status_dict(
                client, "ok", detail="Token refreshed", can_refresh=False
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


def try_silent_refresh(client_id: str) -> dict[str, Any]:
    """
    Attempt silent token refresh for one client.
    Returns status dict plus ok / needs_browser flags.
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
        return result

    if creds and creds.valid:
        result = _status_dict(
            client, "ok", detail="Token already valid", can_refresh=False
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
        return result

    try:
        _refresh_or_raise(creds, client.token)
        result = _status_dict(client, "ok", detail="Token refreshed", can_refresh=False)
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
            return result
        result = _status_dict(
            client,
            "needs_reauth",
            detail=f"Refresh failed: {exc}",
            can_refresh=True,
        )
        result["ok"] = False
        result["needs_browser"] = True
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
        return result


def authorize_client_interactive(client_id: str) -> dict[str, Any]:
    """
    Open Google login via Desktop loopback and save the token.

    Skips token.json / YOUTUBE_REFRESH_TOKEN so a revoked refresh token cannot
    block re-auth. Desktop clients require localhost loopback (not the dashboard
    callback URL).
    """
    client = get_youtube_client(client_id)
    if not client.client_secrets.is_file():
        raise FileNotFoundError(
            f"YouTube client secrets not found for {client.id} at {client.client_secrets}"
        )
    if client.token.is_file():
        _clear_stale_token(client.token)

    logger.info("Opening Google OAuth browser for YouTube client %s", client.id)
    creds = _run_browser_oauth(client.client_secrets)
    _save_token(creds, client.token)
    if client.id == "primary" and creds.refresh_token:
        logger.info(
            "Primary authorized. Update .env YOUTUBE_REFRESH_TOKEN if you use it "
            "(old value was revoked)."
        )

    result = probe_client_status(client, attempt_refresh=False)
    result["ok"] = result.get("status") == "ok"
    result["needs_browser"] = False
    if not result["ok"]:
        result["detail"] = (
            result.get("detail") or "Authorization did not produce a valid token"
        )
    else:
        result["detail"] = "Authorized successfully"
    return result


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
