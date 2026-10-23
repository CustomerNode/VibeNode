"""The model list both layers need: what sessions have confirmed, plus the
models the picker always offers.

The Session Engine records every model a CLI confirms
(:func:`record_confirmed_model`), and falls back to :func:`known_model_ids`
when a session's own CLI does not report the models it can run (the usage
limit auto-switch, ``SessionManager._newest_model_of``).

``app/routes/live_api.py`` imports the lists and delegates
``record_confirmed_model`` here (adding only the web's models-cache
invalidation), so this is the one place to add a new model.
"""

import json
import re

from common.paths import REPO_ROOT

CONFIRMED_MODELS_FILE = REPO_ROOT / "confirmed_models.json"

# Offered even when a same-family model is already known (newly launched ones).
ALWAYS_OFFER_MODELS = [
    {"id": "claude-fable-5-1", "name": "Fable 5.1"},
    {"id": "claude-opus-5-5", "name": "Opus 5.5"},
    {"id": "claude-opus-5", "name": "Opus 5"},
    {"id": "claude-sonnet-5-5", "name": "Sonnet 5.5"},
    {"id": "claude-sonnet-5", "name": "Sonnet 5"},
]

# Used for a family nothing else represents.  Ordered newest first per family.
FALLBACK_KNOWN_MODELS = [
    {"id": "claude-fable-5-1", "name": "Fable 5.1"},
    {"id": "claude-fable-5", "name": "Fable 5"},
    {"id": "claude-opus-5-5", "name": "Opus 5.5"},
    {"id": "claude-opus-5", "name": "Opus 5"},
    {"id": "claude-opus-4-8", "name": "Opus 4.8"},
    {"id": "claude-opus-4-7", "name": "Opus 4.7"},
    {"id": "claude-sonnet-5-5", "name": "Sonnet 5.5"},
    {"id": "claude-sonnet-5", "name": "Sonnet 5"},
    {"id": "claude-haiku-4-5", "name": "Haiku 4.5"},
]

KNOWN_FAMILIES = ("fable", "opus", "sonnet", "haiku")


def load_confirmed_models() -> dict:
    """``{model_id: display_name}`` from the confirmed-models file."""
    try:
        if CONFIRMED_MODELS_FILE.exists():
            return json.loads(CONFIRMED_MODELS_FILE.read_text())
    except Exception:
        pass
    return {}


def _display_name(model_id: str) -> str:
    """``claude-sonnet-4-6-20251022`` -> ``Sonnet 4.6``."""
    name = re.sub(r"-\d{8}$", "", model_id).replace("claude-", "")
    parts = name.split("-")
    family = parts[0].capitalize()
    version = ".".join(parts[1:]) if len(parts) > 1 else ""
    return f"{family} {version}".strip()


def record_confirmed_model(model_id: str) -> None:
    """Add a CLI-confirmed model id to the persistent list.

    The CLI reports ``[1m]`` (and other bracketed markers) on its model id;
    the list stores SDK-valid ids only, so markers are stripped and anything
    that is not ``claude-<name>`` is ignored.  Never raises.
    """
    if not model_id:
        return
    model_id = re.sub(r"\[[^\]]*\]", "", model_id).strip()
    if not re.fullmatch(r"claude-[A-Za-z0-9-]+", model_id):
        return
    confirmed = load_confirmed_models()
    if model_id not in confirmed:
        confirmed[model_id] = _display_name(model_id)
        try:
            CONFIRMED_MODELS_FILE.write_text(json.dumps(confirmed, indent=2))
        except Exception:
            pass


def known_model_ids() -> list:
    """The ids the web's fast model list (``_build_models_quickly``) holds:
    confirmed, plus always-offered, plus a fallback for any family still
    missing.  Order is not meaningful.  Never raises."""
    try:
        ids = list(load_confirmed_models())
    except Exception:
        ids = []
    for extra in ALWAYS_OFFER_MODELS:
        if extra["id"] not in ids:
            ids.append(extra["id"])
    present = {fam for fam in KNOWN_FAMILIES if any(fam in mid for mid in ids)}
    for fallback in FALLBACK_KNOWN_MODELS:
        fam = next((f for f in KNOWN_FAMILIES if f in fallback["id"]), None)
        if fam and fam not in present and fallback["id"] not in ids:
            ids.append(fallback["id"])
            present.add(fam)
    return ids
