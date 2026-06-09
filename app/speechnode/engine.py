"""
SpeechNode engine — local Whisper lifecycle, install orchestration, transcription.

Designed to work on **lots of machines**, not just one:

* **Zero base-install cost.** ``faster-whisper`` is imported lazily, only inside
  functions that need it. Importing this module pulls in no ASR/ML dependency.

* **General, robust install.** ``faster-whisper`` may be impossible to ``pip
  install`` into the running interpreter — e.g. Debian/Ubuntu and Homebrew mark
  the system Python "externally managed" (PEP 668) and refuse global installs.
  So the install tries, in order:
      1. plain ``pip install`` (correct when already inside a virtualenv, or on
         a non-restricted system Python);
      2. a **dedicated, isolated virtualenv that SpeechNode owns**
         (``.cache/speechnode-venv``) whose ``site-packages`` we add to this
         process's ``sys.path`` — sidesteps PEP 668 entirely and changes nothing
         outside SpeechNode;
      3. a ``--user`` install, escalating to ``--break-system-packages`` only as
         a last resort.
  Whatever succeeds is made importable by the running process, and persists
  across restarts (the owned venv is re-activated on demand).

* **Real feedback.** ``start_install()`` runs in a background thread and
  publishes phase/progress/message, plus an error + OS-aware remediation on
  failure. Everything degrades gracefully; the frontend falls back to Web Speech.

Model files AND the owned venv live under ``.cache/`` (gitignored) — nothing
large is ever committed, and paths are derived dynamically (never hardcoded).
"""

from __future__ import annotations

import glob
import importlib
import importlib.util
import logging
import os
import subprocess
import sys
import threading
from pathlib import Path

logger = logging.getLogger("app.speechnode")

# Default model: base.en — ~145 MB, English, native punctuation, fast on CPU.
DEFAULT_MODEL = os.environ.get("SPEECHNODE_MODEL", "base.en")

_STATE = {
    "phase": "idle",        # idle | installing | downloading | loading | ready | error
    "message": "",
    "error": "",
    "remediation": "",
    "model": DEFAULT_MODEL,
    "progress": 0,
}
_LOCK = threading.Lock()
_INFER_LOCK = threading.Lock()   # serialize transcription (streaming sends overlapping requests)
_MODEL = None
_INSTALL_THREAD = None


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _model_dir() -> str:
    override = os.environ.get("SPEECHNODE_MODEL_DIR")
    d = Path(override) if override else _repo_root() / ".cache" / "speechnode-models"
    d.mkdir(parents=True, exist_ok=True)
    return str(d)


def _local_venv_dir() -> Path:
    override = os.environ.get("SPEECHNODE_VENV_DIR")
    return Path(override) if override else _repo_root() / ".cache" / "speechnode-venv"


def _venv_python(venv: Path) -> Path:
    if os.name == "nt":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


def _venv_site_packages(venv: Path) -> list:
    pats = [str(venv / "lib" / "python*" / "site-packages"),
            str(venv / "Lib" / "site-packages")]
    out = []
    for p in pats:
        out += [d for d in glob.glob(p) if os.path.isdir(d)]
    return out


def _activate_local_venv() -> None:
    """Make a previously-created SpeechNode venv importable by THIS process."""
    for sp in _venv_site_packages(_local_venv_dir()):
        if sp not in sys.path:
            sys.path.insert(0, sp)


def _add_user_site() -> None:
    try:
        import site
        us = site.getusersitepackages()
        if us and us not in sys.path:
            sys.path.insert(0, us)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# State helpers
# --------------------------------------------------------------------------- #
def _set_state(**kw) -> None:
    with _LOCK:
        _STATE.update(kw)


def get_status() -> dict:
    with _LOCK:
        snap = dict(_STATE)
    snap["deps_available"] = _deps_available()
    snap["ready"] = is_ready()
    snap["model_dir"] = _model_dir()
    return snap


def is_ready() -> bool:
    return _MODEL is not None


def system_check() -> dict:
    """
    Cheap machine-suitability check used to decide whether to even *suggest*
    SpeechNode: free disk + CPU cores. (Empirical CPU benchmarking happens
    post-install; this is the pre-install gate.) Never raises.
    """
    import platform as _platform
    import shutil as _shutil
    required_gb = 0.7       # actual footprint: model + isolated venv
    min_free_gb = 1.5       # require this much headroom to recommend
    free_gb = None
    try:
        free_gb = round(_shutil.disk_usage(str(_repo_root())).free / (1024 ** 3), 1)
    except Exception:
        pass
    cores = os.cpu_count() or 1
    enough_disk = (free_gb is not None) and (free_gb >= min_free_gb)
    cpu_ok = cores >= 4
    return {
        "free_gb": free_gb,
        "required_gb": required_gb,
        "cpu_cores": cores,
        "platform": _platform.system(),
        "enough_disk": enough_disk,
        "recommended": bool(enough_disk and cpu_ok),
    }


def _deps_importable() -> bool:
    importlib.invalidate_caches()
    try:
        return importlib.util.find_spec("faster_whisper") is not None
    except Exception:
        return False


def _deps_available() -> bool:
    """Importable now? Activates any owned venv first (covers restarts)."""
    _activate_local_venv()
    return _deps_importable()


# --------------------------------------------------------------------------- #
# Install / download orchestration
# --------------------------------------------------------------------------- #
def start_install(model: str | None = None) -> dict:
    """Kick off (or resume) install in a background thread. Idempotent."""
    global _INSTALL_THREAD
    model = model or DEFAULT_MODEL
    with _LOCK:
        if _STATE["phase"] in ("installing", "downloading", "loading"):
            return dict(_STATE)
        if is_ready() and _STATE["model"] == model:
            _STATE["phase"] = "ready"
            return dict(_STATE)
        _STATE.update(phase="installing", message="Preparing SpeechNode…",
                      error="", remediation="", model=model, progress=2)
    _INSTALL_THREAD = threading.Thread(
        target=_run_install, args=(model,), daemon=True, name="speechnode-install")
    _INSTALL_THREAD.start()
    return get_status()


def _run(cmd, timeout=1800):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _pip(args, py=None):
    return _run([py or sys.executable, "-m", "pip", *args])


def _tail(proc, n=4) -> str:
    txt = (getattr(proc, "stderr", "") or getattr(proc, "stdout", "") or "").strip()
    return "\n".join(txt.splitlines()[-n:])


def _remediation_text() -> str:
    venv = _local_venv_dir()
    vpip = _venv_python(venv).parent / ("pip.exe" if os.name == "nt" else "pip")
    return (
        "SpeechNode couldn't set up its engine automatically (pip may be restricted "
        "on this system, or the machine is offline).\n\n"
        "Fix it with one of these, then restart the web server and re-enable SpeechNode:\n\n"
        "  Recommended — isolated environment (no changes to system Python):\n"
        f"      {sys.executable} -m venv \"{venv}\"\n"
        f"      \"{vpip}\" install faster-whisper\n\n"
        "  Or install into your user account:\n"
        f"      {sys.executable} -m pip install --user faster-whisper\n"
        "      (add --break-system-packages if your OS requires it)"
    )


def _try_dedicated_venv():
    """Create/own an isolated venv and install faster-whisper into it."""
    venv = _local_venv_dir()
    vpy = _venv_python(venv)
    try:
        if not vpy.exists():
            _set_state(message="Creating an isolated environment for SpeechNode…", progress=12)
            r = _run([sys.executable, "-m", "venv", str(venv)], timeout=300)
            if r.returncode != 0 or not vpy.exists():
                return False, "venv: " + (_tail(r) or "creation failed (is python3-venv installed?)")
        _run([str(vpy), "-m", "ensurepip", "--upgrade"], timeout=300)  # best effort
        _set_state(message="Installing the SpeechNode engine (isolated)…", progress=24)
        r = _pip(["install", "--upgrade", "faster-whisper"], py=str(vpy))
        if r.returncode != 0:
            return False, "venv: " + (_tail(r) or "pip install failed")
        _activate_local_venv()
        return (_deps_importable(), "venv: installed but not importable")
    except Exception as e:  # noqa: BLE001
        return False, "venv: " + str(e)


def _ensure_dependency() -> bool:
    """Make ``faster-whisper`` importable by this process. General + layered."""
    if _deps_available():
        return True
    errors = []

    # 1) Plain install — correct inside a virtualenv or a non-restricted Python.
    _set_state(message="Installing the SpeechNode engine…", progress=10)
    try:
        r = _pip(["install", "--upgrade", "faster-whisper"])
        if r.returncode == 0 and _deps_importable():
            return True
        errors.append(_tail(r))
    except Exception as e:  # noqa: BLE001
        errors.append(str(e))

    # 2) Dedicated isolated venv — general fix for PEP 668 / locked system Python.
    ok, err = _try_dedicated_venv()
    if ok:
        return True
    if err:
        errors.append(err)

    # 3) User-site, escalating to the PEP 668 override only as a last resort.
    for extra in (["--user"], ["--user", "--break-system-packages"]):
        _set_state(message="Installing the SpeechNode engine (user site)…", progress=34)
        try:
            r = _pip(["install", "--upgrade", "faster-whisper", *extra])
            if r.returncode == 0:
                _add_user_site()
                if _deps_importable():
                    return True
            errors.append(_tail(r))
        except Exception as e:  # noqa: BLE001
            errors.append(str(e))

    _set_state(phase="error",
               error="Could not install the SpeechNode engine automatically on this machine.",
               remediation=_remediation_text())
    logger.warning("SpeechNode dep install failed: %s", (" | ".join(e for e in errors if e))[-800:])
    return False


def _run_install(model: str) -> None:
    """Background worker: ensure dep, download+load the model, mark ready."""
    global _MODEL
    try:
        if not _ensure_dependency():
            return  # remediation already recorded
        _set_state(phase="downloading", message=f"Downloading model '{model}' (~one-time)…", progress=60)
        _activate_local_venv()
        from faster_whisper import WhisperModel  # lazy
        _set_state(phase="loading", message="Loading model into memory…", progress=88)
        mdl = WhisperModel(model, device="cpu", compute_type="int8", download_root=_model_dir())
        with _LOCK:
            _MODEL = mdl
        _set_state(phase="ready", message="SpeechNode is ready.", progress=100, error="", remediation="")
        logger.info("SpeechNode model '%s' ready.", model)
    except Exception as e:  # noqa: BLE001
        logger.warning("SpeechNode install failed: %s", e)
        _set_state(phase="error",
                   error=f"Could not load the SpeechNode model: {e}",
                   remediation="Check your internet connection and free disk space, then try again.\n\n"
                               + _remediation_text())


# --------------------------------------------------------------------------- #
# Transcription
# --------------------------------------------------------------------------- #
def get_model():
    global _MODEL
    if _MODEL is not None:
        return _MODEL
    if not _deps_available():
        raise RuntimeError("SpeechNode engine is not installed.")
    from faster_whisper import WhisperModel  # lazy
    with _LOCK:
        if _MODEL is None:
            _MODEL = WhisperModel(_STATE["model"], device="cpu", compute_type="int8",
                                  download_root=_model_dir())
            _STATE["phase"] = "ready"
    return _MODEL


def transcribe(audio_path: str, initial_prompt: str | None = None,
               language: str = "en", fast: bool = False) -> str:
    """
    Transcribe audio. ``initial_prompt`` carries the codebase bias terms.

    ``fast=True`` is the low-latency path for streaming partials: greedy decode
    (beam_size=1) and no cross-segment conditioning, so each live update returns
    as quickly as possible. The final pass (fast=False) uses the higher-quality
    settings, so the committed transcript is unchanged.
    """
    model = get_model()
    # Serialize inference: streaming sends overlapping partial+final requests, and
    # the underlying model isn't guaranteed safe to call concurrently.
    with _INFER_LOCK:
        segments, info = model.transcribe(
            audio_path,
            language=language or None,
            initial_prompt=(initial_prompt or None),
            vad_filter=True,
            beam_size=(1 if fast else 5),
            condition_on_previous_text=(not fast),
        )
        seg_list = list(segments)  # materialize the generator inside the lock
    text = " ".join(s.text.strip() for s in seg_list if s.text and s.text.strip()).strip()
    # Trailing-silence gap = audio duration minus the end of the last SPEECH segment.
    # Whisper's VAD ignores non-speech, so this is real "silence since you stopped",
    # immune to ambient noise — the robust end-of-speech signal for streaming.
    last_end = float(seg_list[-1].end) if seg_list else 0.0
    duration = float(getattr(info, "duration", 0.0) or 0.0)
    gap = round(max(0.0, duration - last_end), 2)
    return {"text": text, "gap": gap, "duration": round(duration, 2)}
