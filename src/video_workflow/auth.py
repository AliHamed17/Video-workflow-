"""One-time OAuth helper: get a YouTube refresh token via the loopback redirect flow."""

from __future__ import annotations

import secrets
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 — a URL, not a secret
YOUTUBE_SCOPES = ("https://www.googleapis.com/auth/youtube.upload "
                  "https://www.googleapis.com/auth/youtube.readonly "                 # upload + read stats
                  "https://www.googleapis.com/auth/yt-analytics-monetary.readonly")  # revenue per video


def youtube_refresh_token(client_id: str, client_secret: str, port: int = 8765,
                          open_browser: bool = True) -> str:
    """Run the consent flow for a *Desktop app* OAuth client and return the refresh token."""
    redirect_uri = f"http://127.0.0.1:{port}/"
    state = secrets.token_urlsafe(16)
    url = AUTH_URL + "?" + urlencode({
        "client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code",
        "scope": YOUTUBE_SCOPES, "access_type": "offline", "prompt": "consent",
        "state": state,
    })
    captured: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server API
            query = parse_qs(urlparse(self.path).query)
            captured.update({k: v[0] for k, v in query.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Authorized. You can close this tab and return to the terminal.")

        def log_message(self, *args):
            pass

    print(f"Open this URL to authorize YouTube uploads:\n\n{url}\n")
    if open_browser:
        webbrowser.open(url)
    with HTTPServer(("127.0.0.1", port), Handler) as server:
        while "code" not in captured and "error" not in captured:
            server.handle_request()
    if captured.get("error"):
        raise RuntimeError(f"Authorization failed: {captured['error']}")
    if captured.get("state") != state:
        raise RuntimeError("OAuth state mismatch — aborting")

    resp = httpx.post(TOKEN_URL, data={
        "code": captured["code"], "client_id": client_id, "client_secret": client_secret,
        "redirect_uri": redirect_uri, "grant_type": "authorization_code",
    }, timeout=30)
    resp.raise_for_status()
    token = resp.json().get("refresh_token")
    if not token:
        raise RuntimeError("Google returned no refresh_token (revoke the app's access and retry)")
    return token
