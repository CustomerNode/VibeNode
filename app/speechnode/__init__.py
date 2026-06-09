"""
SpeechNode — VibeNode's local, codebase-aware voice engine.

Everything in this package is OPT-IN and lazy-loaded. None of it imports the
heavy ASR dependency (``faster-whisper``) at module import time, so the base
VibeNode install is completely unaffected for users who never enable SpeechNode.

Public surface:
    engine       — model lifecycle, install orchestration, transcription
    knowledge    — Codebase Knowledge Layer (vocabulary + bias prompt)
    postprocess  — disfluency / restart cleanup + term correction

The HTTP surface lives in ``app/routes/speechnode_api.py`` (``/api/speechnode/...``).
"""
