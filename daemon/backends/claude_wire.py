"""The Claude CLI's stream-json vocabulary, as a Session Host needs it.

SDK-free on purpose (like ``claude_layout.py``): the host process
(``daemon/session_host.py``) imports this and nothing from the SDK, so it
starts fast and stays small (no ``claude_code_sdk``, ``mcp``, ``pydantic``).
The host parses each CLI line only for these few facts.
"""

from __future__ import annotations

from typing import Optional


def init_request_id(msg: dict) -> Optional[str]:
    """The request id of an SDK ``initialize`` control request, else None."""
    if msg.get("type") == "control_request" and \
            (msg.get("request") or {}).get("subtype") == "initialize":
        return msg.get("request_id")
    return None


def response_request_id(msg: dict) -> Optional[str]:
    """The request id a ``control_response`` answers, else None."""
    if msg.get("type") == "control_response":
        return (msg.get("response") or {}).get("request_id")
    return None


def cli_request_id(msg: dict) -> Optional[str]:
    """The id of a control request the CLI sent to the SDK (permission,
    hook callback, MCP message), which the CLI waits on until answered."""
    if msg.get("type") == "control_request":
        return msg.get("request_id")
    return None


def is_turn_end(msg: dict) -> bool:
    return msg.get("type") == "result"


def is_turn_activity(msg: dict) -> bool:
    """Output that means a turn is running (also one the CLI started itself,
    e.g. a scheduled wake-up or a background-task notification)."""
    return msg.get("type") in ("assistant", "stream_event", "user")


def is_user_input(msg: dict) -> bool:
    """An SDK -> CLI line that starts a turn."""
    return msg.get("type") == "user"


def cached_init_answer(cached_response: dict, request_id: str) -> dict:
    """The CLI's first ``initialize`` answer, re-addressed to a later request."""
    resp = dict(cached_response)
    resp["request_id"] = request_id
    return {"type": "control_response", "response": resp}


def dedup_key(msg: dict) -> Optional[str]:
    """Replayed transcript messages carry the CLI's ``uuid``; a restarted
    engine that already loaded that message from the transcript skips it."""
    if msg.get("type") in ("assistant", "user"):
        return msg.get("uuid") or None
    return None
