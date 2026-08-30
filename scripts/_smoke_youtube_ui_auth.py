"""Smoke: YouTube client probe order + silent refresh needs_browser."""

from __future__ import annotations

from src.youtube.auth import (
    DEFAULT_WEB_REDIRECT_URI,
    build_web_oauth_authorization,
    get_youtube_client,
    list_youtube_clients,
    probe_youtube_clients,
    try_silent_refresh,
)


def main() -> None:
    configured = [c.id for c in list_youtube_clients()]
    probed = probe_youtube_clients(attempt_refresh=False)
    ids = [p["id"] for p in probed]
    assert ids == configured, (ids, configured)
    assert "primary" in ids and "backup1" in ids and "backup2" in ids
    for row in probed:
        assert "status" in row and "can_refresh" in row
        assert row["status"] in {
            "ok",
            "expired",
            "missing_token",
            "missing_secrets",
            "needs_reauth",
            "error",
        }
    print("PROBE ok", [(r["id"], r["status"]) for r in probed])

    result = try_silent_refresh("primary")
    assert "ok" in result and "needs_browser" in result and "status" in result
    print("REFRESH_SHAPE ok", result["id"], result["status"], result["ok"], result["needs_browser"])

    # needs_browser: temporarily hide primary token without deleting it
    client = get_youtube_client("primary")
    token = client.token
    hidden = token.with_suffix(token.suffix + ".smoke_bak")
    assert token.is_file(), "primary token missing"
    token.replace(hidden)
    try:
        missing = try_silent_refresh("primary")
        assert missing["ok"] is False
        assert missing["needs_browser"] is True
        assert missing["status"] == "missing_token"
        print("NEEDS_BROWSER ok", missing["status"])

        started = build_web_oauth_authorization(
            "primary", redirect_uri=DEFAULT_WEB_REDIRECT_URI
        )
        assert "auth_url" in started and "state" in started and "code_verifier" in started
        assert "accounts.google.com" in started["auth_url"]
        assert "code_challenge" in started["auth_url"]
        assert DEFAULT_WEB_REDIRECT_URI in started["auth_url"] or "127.0.0.1" in started["auth_url"]
        print("AUTHORIZE_URL ok", started["redirect_uri"])
    finally:
        if hidden.is_file():
            hidden.replace(token)

    try:
        try_silent_refresh("does-not-exist")
        raise AssertionError("expected ValueError")
    except ValueError:
        print("UNKNOWN_CLIENT ok")

    print("STATUS=ok")


if __name__ == "__main__":
    main()
