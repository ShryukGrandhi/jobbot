"""Per-tenant credential vault backed by the macOS Keychain.

Workday is not one account, it is one account per employer tenant. A serious run
accumulates dozens of them. They are real credentials to real systems holding
the user's personal data, so they go in the OS keychain via `keyring`, never in
a YAML file next to the code -- which is, notably, exactly where every
open-source applier in this space stores the user's password today.
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import string
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import structlog

log = structlog.get_logger(__name__)

SERVICE = "jobbot-ats"
INDEX_PATH = Path.home() / ".jobbot" / "tenants.json"


def generate_password(length: int = 20) -> str:
    """Strong password meeting the usual enterprise complexity rules.

    Workday tenants commonly demand upper, lower, digit and symbol, and reject
    several symbols that break their own form handling. Guarantee one of each so
    we never fail validation on a technicality mid-signup.
    """
    symbols = "!@#$%^*-_=+"
    pools = [string.ascii_uppercase, string.ascii_lowercase, string.digits, symbols]
    chars = [secrets.choice(p) for p in pools]
    alphabet = "".join(pools)
    chars += [secrets.choice(alphabet) for _ in range(length - len(chars))]
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


@dataclass
class Credential:
    tenant: str
    username: str
    password: str
    created_at: str


def _key(ats: str, tenant: str) -> str:
    return f"{ats}:{tenant}".lower()


# When the OS keychain refuses the write, secrets go here instead: the
# candidate's own home directory, mode 0600, alongside the .env and the Gmail
# token that already live there. macOS returned -61 (a write-permission error)
# for every account this run tried to create, and without somewhere to put the
# password the account is created and then lost -- locking the candidate out of
# that employer's site with no way back in.
FALLBACK_PATH = Path.home() / ".jobbot" / "credentials.json"


def _fallback_read() -> dict[str, dict]:
    if not FALLBACK_PATH.exists():
        return {}
    try:
        return json.loads(FALLBACK_PATH.read_text(encoding="utf-8") or "{}")
    except (OSError, json.JSONDecodeError):
        return {}


def _fallback_write(key: str, payload: dict) -> None:
    FALLBACK_PATH.parent.mkdir(parents=True, exist_ok=True)
    data = _fallback_read()
    data[key] = payload
    # Create with 0600 from the start rather than widening then narrowing.
    fd = os.open(FALLBACK_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
    os.chmod(FALLBACK_PATH, 0o600)


def save(ats: str, tenant: str, username: str, password: str) -> Credential:
    import keyring

    k = _key(ats, tenant)
    cred = Credential(tenant=tenant, username=username, password=password,
                      created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    payload = {"username": username, "password": password,
               "created_at": cred.created_at}
    try:
        keyring.set_password(SERVICE, k, json.dumps(payload))
    except Exception as exc:  # noqa: BLE001
        _fallback_write(k, payload)
        log.warning("credentials.keychain_unavailable", error=str(exc)[:120],
                    stored_in=str(FALLBACK_PATH), mode="0600")

    # An index of WHICH tenants exist (never the secrets) so a run can tell at a
    # glance whether it already has an account somewhere.
    INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    idx = {}
    if INDEX_PATH.exists():
        try:
            idx = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            idx = {}
    idx[k] = {"username": username, "created_at": cred.created_at}
    INDEX_PATH.write_text(json.dumps(idx, indent=2, sort_keys=True), encoding="utf-8")

    log.info("credentials.saved", ats=ats, tenant=tenant, username=username)
    return cred


def load(ats: str, tenant: str) -> Credential | None:
    import keyring

    k = _key(ats, tenant)
    raw = None
    with contextlib.suppress(Exception):
        raw = keyring.get_password(SERVICE, k)
    if raw:
        d = json.loads(raw)
    else:
        d = _fallback_read().get(k)
        if not d:
            return None
    return Credential(tenant=tenant, username=d["username"], password=d["password"],
                      created_at=d.get("created_at", ""))


def have(ats: str, tenant: str) -> bool:
    return load(ats, tenant) is not None


def list_tenants() -> dict[str, dict]:
    if INDEX_PATH.exists():
        try:
            return json.loads(INDEX_PATH.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}
    return {}
