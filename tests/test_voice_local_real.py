"""The on-device engine with the REAL model: opt-in, it downloads ~80 MB once.

Run with ``VIBENODE_VOICE_LOCAL_REAL=1`` on Windows (the clip is spoken by the
PC's own voice). Proves ``src/web/static/js/voice-local.js`` end to end in a real
browser: library from the CDN, model from the hub, a recorded clip decoded and
transcribed inside the page. Skipped otherwise, so a plain suite run never
reaches the network for it.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LOCAL = ROOT / "src" / "web" / "static" / "js" / "voice-local.js"
SENTENCE = ("Fix the login page so that the header lines up with the sidebar, "
            "then run the tests and tell me what failed.")

pytestmark = pytest.mark.skipif(
    os.environ.get("VIBENODE_VOICE_LOCAL_REAL") != "1" or sys.platform != "win32",
    reason="opt-in: VIBENODE_VOICE_LOCAL_REAL=1 on Windows (downloads the model)")


def _clip(tmp_path):
    wav = tmp_path / "clip.wav"
    ps = ("Add-Type -AssemblyName System.Speech; $s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
          f"$s.SetOutputToWaveFile('{wav}'); $s.Speak('{SENTENCE}'); $s.Dispose()")
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True, timeout=120)
    return wav.read_bytes()


def test_the_real_model_transcribes_a_clip_inside_the_browser(tmp_path):
    pw = pytest.importorskip("playwright.sync_api")
    clip = _clip(tmp_path)
    page_html = ("<!doctype html><script src='/voice-local.js'></script><script>"
                 "window.__go = async () => { const b = await (await fetch('/clip.wav')).blob();"
                 " return await VoiceLocal.transcribeBlob(b); };</script>")
    with pw.sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page()
        pg.route("http://vn.test/", lambda r: r.fulfill(body=page_html, content_type="text/html"))
        pg.route("http://vn.test/voice-local.js", lambda r: r.fulfill(body=LOCAL.read_bytes(), content_type="text/javascript"))
        pg.route("http://vn.test/clip.wav", lambda r: r.fulfill(body=clip, content_type="audio/wav"))
        pg.goto("http://vn.test/")
        res = pg.evaluate("window.__go()")   # Playwright waits for the promise
        b.close()
    assert res["ok"], res
    norm = lambda s: "".join(ch for ch in s.lower() if ch.isalnum() or ch == " ").split()
    got, want = norm(res["text"]), norm(SENTENCE)
    hits = sum(1 for a, b in zip(got, want) if a == b)
    # A synthesized voice is not a person: allow a word or two off (seen: "test" for "tests").
    assert len(got) == len(want) and hits >= len(want) - 2, res["text"]
