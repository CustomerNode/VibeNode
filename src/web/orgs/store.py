"""This VibeNode's org membership and the calls it makes.

The membership file (``data/vibenode_orgs.json``, gitignored, mode 600) holds
the project URL, its publishable key and this member's token. The token is the
member's only credential: it is never sent to the browser (``public_view``) and
never logged. Every call goes to a database function that checks it.
"""

from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path

from ..config import _VIBENODE_DIR

MEMBERSHIP_FILE: Path = Path(os.environ.get("VIBENODE_ORGS_FILE")
                             or (_VIBENODE_DIR / "data" / "vibenode_orgs.json"))
_lock = threading.Lock()
_URL_RE = re.compile(r"^https://[a-z0-9-]+\.supabase\.(co|in)$")


class OrgsError(Exception):
    """A refused or failed call. ``code`` is the database's reason
    (``not_a_member``, ``invite_invalid``, ``no_access``…) or ``network``."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


def valid_url(url: str) -> bool:
    return bool(_URL_RE.match((url or "").strip().rstrip("/")))


def load() -> dict:
    try:
        data = json.loads(MEMBERSHIP_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) and data.get("token") else {}
    except (OSError, ValueError):
        return {}


def save(data: dict) -> None:
    with _lock:
        MEMBERSHIP_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = MEMBERSHIP_FILE.parent / (MEMBERSHIP_FILE.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, MEMBERSHIP_FILE)


def clear() -> None:
    with _lock:
        try:
            MEMBERSHIP_FILE.unlink()
        except OSError:
            pass


def public_view(data: dict | None = None) -> dict:
    """What the browser may see: everything but the token and the key."""
    data = load() if data is None else data
    if not data:
        return {"member": False}
    return {"member": True, **{k: data.get(k) for k in
            ("org_id", "org_name", "member_id", "username", "role", "url")}}


_ERR_RE = re.compile(r"\b(not_a_member|not_allowed|bad_days|invite_invalid|username_taken|"
                     r"not_a_member_of_your_org|bad_minutes|not_your_session|too_many|bad_kind|"
                     r"no_access|bad_text|permission denied)\b")


def rpc(fn: str, params: dict, *, url: str | None = None, key: str | None = None,
        timeout: float = 15.0):
    """Call database function ``fn`` with named ``params``.

    Uses the saved membership's project unless ``url`` / ``key`` are given (the
    join and setup paths, before a membership exists)."""
    import httpx

    if url is None or key is None:
        m = load()
        if not m:
            raise OrgsError("not_set_up", "This VibeNode is not in an org.")
        url, key = m["url"], m["key"]
    if not valid_url(url):
        raise OrgsError("bad_url", "Not a Supabase project address.")
    try:
        resp = httpx.post(f"{url.rstrip('/')}/rest/v1/rpc/{fn}", json=params, timeout=timeout,
                          headers={"apikey": key, "Authorization": f"Bearer {key}",
                                   "Content-Type": "application/json"})
    except httpx.HTTPError as e:
        raise OrgsError("network", f"Could not reach the org's cloud storage: {type(e).__name__}")
    if resp.status_code >= 400:
        text = resp.text[:400]
        m = _ERR_RE.search(text)
        if resp.status_code == 404 and "Could not find the function" in text:
            raise OrgsError("schema_missing", "VibeNode Orgs is not set up in this cloud storage yet.")
        raise OrgsError(m.group(1).replace(" ", "_") if m else f"http_{resp.status_code}", text)
    try:
        return resp.json()
    except ValueError:
        return None


def call(fn: str, **params):
    """Call ``fn`` as this member (the saved token is passed as ``p_token``)."""
    m = load()
    if not m:
        raise OrgsError("not_set_up", "This VibeNode is not in an org.")
    return rpc(fn, {"p_token": m["token"], **params}, url=m["url"], key=m["key"])
