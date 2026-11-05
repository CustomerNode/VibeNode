"""The copyable join prompt for a new org member.

The owner (or an admin) makes a single-use code that expires in N days and
copies a prompt to send to the friend. The friend pastes it into their own
VibeNode; the session it lands in posts the block to ``/api/orgs/join`` with a
username, and the friend is in.

What the prompt carries: the cloud storage address, its PUBLISHABLE key (made
to be public; every table is closed to it and only the checked functions are
open), the org's name, who invited them, the expiry, and the code. It never
carries the project's secret key. The code works once: redeeming it marks it
used in the same database transaction, so a forwarded or replayed prompt fails.
"""

from __future__ import annotations

import json
import re

FENCE = "vibenode-orgs-invite"
_BLOCK_RE = re.compile(r"```" + FENCE + r"\s*\n(.*?)\n```", re.S)
_CODE_RE = re.compile(r"^[A-Za-z0-9_-]{40,64}$")


def build_prompt(*, url: str, key: str, code: str, org_name: str, inviter: str,
                 expires_at: str) -> str:
    block = json.dumps({"v": 1, "url": url, "key": key, "code": code, "org": org_name,
                        "invited_by": inviter, "expires_at": expires_at}, indent=2)
    return (
        f"{inviter} invited you to the VibeNode org \"{org_name}\" so you can Phone a Friend: "
        "open a session to each other when one of you is stuck.\n\n"
        "To the VibeNode session reading this: ask me which username I want in the org "
        "(letters, numbers, spaces, dots, dashes; up to 32), then join by running exactly:\n\n"
        "curl -s -X POST http://127.0.0.1:5050/api/orgs/join -H 'Content-Type: application/json' "
        "-d '{\"username\": \"<the name I chose>\", \"invite\": <the JSON block below>}'\n\n"
        "and tell me what it answered. The invite works once and expires "
        f"{expires_at[:10]}. Do not post it anywhere else.\n\n"
        f"```{FENCE}\n{block}\n```\n"
    )


def parse(value) -> dict:
    """The invite dict from a prompt, a block or a dict. Raises ValueError."""
    if isinstance(value, str):
        m = _BLOCK_RE.search(value)
        value = json.loads(m.group(1) if m else value)
    if not isinstance(value, dict):
        raise ValueError("not an invite")
    out = {k: str(value.get(k) or "").strip() for k in ("url", "key", "code", "org", "invited_by")}
    if not (out["url"] and out["key"] and _CODE_RE.match(out["code"])):
        raise ValueError("the invite is missing its address, key or code")
    return out
