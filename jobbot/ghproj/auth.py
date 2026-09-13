"""GitHub authorization.

Three sources, in order of preference:

  1. An explicit OAuth device-flow grant (needs a registered OAuth app client id).
  2. GITHUB_TOKEN / GH_TOKEN from the environment.
  3. The token already held by the `gh` CLI.

Whichever is used, `ensure_consent()` must pass first. This system creates
public repositories under the user's name and pushes commits as them -- that is
their public professional record, and it should never happen as a silent side
effect of running a job search. Consent is recorded to disk with a timestamp and
the scopes actually held.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx
import structlog

log = structlog.get_logger(__name__)

CONSENT_PATH = Path.home() / ".jobbot" / "github_consent.json"
DEVICE_CODE_URL = "https://github.com/login/device/code"
ACCESS_TOKEN_URL = "https://github.com/login/oauth/access_token"
API = "https://api.github.com"

# Creating a repo and pushing to it needs `repo`. `read:user` identifies the account.
REQUIRED_SCOPES = {"repo"}


class GitHubAuthError(RuntimeError):
    pass


@dataclass
class GitHubIdentity:
    login: str
    token: str
    scopes: set[str]
    source: str

    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }


def _gh_cli_token() -> str | None:
    try:
        r = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=10)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except Exception:  # noqa: BLE001
        pass
    return None


def device_flow(client_id: str, scopes: str = "repo read:user") -> str:
    """Full OAuth device flow. Prints the code for the user to enter."""
    r = httpx.post(DEVICE_CODE_URL, data={"client_id": client_id, "scope": scopes},
                   headers={"Accept": "application/json"}, timeout=30)
    r.raise_for_status()
    d = r.json()

    print("\n" + "=" * 66)
    print("  GitHub authorization required")
    print(f"  1. Open: {d['verification_uri']}")
    print(f"  2. Enter code: {d['user_code']}")
    print("=" * 66 + "\n", flush=True)

    interval = int(d.get("interval", 5))
    deadline = time.time() + int(d.get("expires_in", 900))
    while time.time() < deadline:
        time.sleep(interval)
        t = httpx.post(ACCESS_TOKEN_URL, headers={"Accept": "application/json"}, data={
            "client_id": client_id, "device_code": d["device_code"],
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        }, timeout=30).json()
        if "access_token" in t:
            return t["access_token"]
        err = t.get("error")
        if err == "authorization_pending":
            continue
        if err == "slow_down":
            interval += 5
            continue
        raise GitHubAuthError(f"device flow failed: {t}")
    raise GitHubAuthError("device flow timed out")


def _identify(token: str, source: str) -> GitHubIdentity:
    r = httpx.get(f"{API}/user", timeout=20, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    })
    if r.status_code != 200:
        raise GitHubAuthError(f"token rejected by GitHub ({r.status_code})")
    scopes = {s.strip() for s in r.headers.get("x-oauth-scopes", "").split(",") if s.strip()}
    return GitHubIdentity(login=r.json()["login"], token=token, scopes=scopes, source=source)


def get_identity(client_id: str | None = None) -> GitHubIdentity:
    client_id = client_id or os.environ.get("GITHUB_OAUTH_CLIENT_ID")
    if client_id:
        return _identify(device_flow(client_id), "device_flow")

    tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if tok:
        return _identify(tok, "env")

    tok = _gh_cli_token()
    if tok:
        return _identify(tok, "gh_cli")

    raise GitHubAuthError(
        "no GitHub credential. Set GITHUB_OAUTH_CLIENT_ID for device flow, "
        "or GITHUB_TOKEN, or run `gh auth login`."
    )


def consent_record() -> dict | None:
    if CONSENT_PATH.exists():
        try:
            return json.loads(CONSENT_PATH.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return None
    return None


def grant_consent(identity: GitHubIdentity, *, public: bool) -> dict:
    CONSENT_PATH.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "login": identity.login,
        "scopes": sorted(identity.scopes),
        "source": identity.source,
        "public_repos_allowed": public,
        "granted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    CONSENT_PATH.write_text(json.dumps(rec, indent=2), encoding="utf-8")
    log.info("github.consent_recorded", login=identity.login, public=public)
    return rec


def ensure_consent(identity: GitHubIdentity, *, interactive: bool = True,
                   public: bool = True) -> dict:
    """Require a recorded, matching consent before any repo is created."""
    rec = consent_record()
    if rec and rec.get("login") == identity.login:
        return rec

    missing = REQUIRED_SCOPES - identity.scopes
    if missing and identity.source != "device_flow":
        raise GitHubAuthError(
            f"token for {identity.login} lacks required scope(s): {sorted(missing)}"
        )

    if not interactive:
        raise GitHubAuthError(
            f"no recorded consent for {identity.login}. Run `jobbot github-auth` once."
        )

    print("\n" + "=" * 66)
    print("  GitHub authorization")
    print(f"  Account : {identity.login}   (via {identity.source})")
    print(f"  Scopes  : {', '.join(sorted(identity.scopes)) or '(unknown)'}")
    print()
    print("  This will create REAL repositories on your account and push real")
    print("  commits as you. They become part of your public record, and a")
    print("  hiring manager may read them. Nothing is backdated or faked.")
    print("=" * 66)
    resp = input("  Type 'yes' to authorize: ").strip().lower()
    if resp != "yes":
        raise GitHubAuthError("GitHub authorization declined by the user")
    return grant_consent(identity, public=public)
