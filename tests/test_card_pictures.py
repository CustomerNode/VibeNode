"""A summary's pictures survive the session deleting or moving them (otherwise
they draw as 404s in the summaries that showed them).

When a summary passes the stop, every picture it shows that is a file on this
machine is copied into the store (src/shared/card_pictures.py), and GET
/api/card-picture serves the copy; the card asks for it when the original
fails to load (last-card.js)."""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from shared import card_pictures as cp  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("VIBENODE_CARD_PICTURES", "1")
    monkeypatch.setenv("VIBENODE_CARD_PICTURES_DIR", str(tmp_path / "store"))
    return tmp_path


def _card(url, title="Picture card", headline="It shows a picture"):
    return {"type": "done", "title": title, "headline": headline,
            "links": [{"label": "shot", "kind": "image", "url": url}]}


def test_a_deleted_picture_is_still_served(store):
    shot = store / "shot.png"
    shot.write_bytes(PNG)
    card = _card(str(shot))
    assert cp.save_card(card) == 1
    shot.unlink()
    got = cp.find(str(shot), cp.card_key(card["title"], card["headline"]))
    assert got is not None and got.read_bytes() == PNG
    # The card's own address for it (through /api/card-image) finds it too.
    assert cp.find("/api/card-image?path=" + str(shot)) == got


def test_an_overwritten_picture_keeps_what_each_summary_saw(store):
    shot = store / "same-name.png"
    shot.write_bytes(PNG + b"first")
    cp.save_card(_card(str(shot), title="First"))
    shot.write_bytes(PNG + b"second")
    cp.save_card(_card(str(shot), title="Second"))
    shot.unlink()
    assert cp.find(str(shot), cp.card_key("First", "It shows a picture")).read_bytes().endswith(b"first")
    assert cp.find(str(shot), cp.card_key("Second", "It shows a picture")).read_bytes().endswith(b"second")
    assert cp.find(str(shot)).read_bytes().endswith(b"second")   # unknown summary: the newest


def test_screens_pictures_are_saved_and_non_pictures_are_not(store):
    pic, txt = store / "phone.png", store / "notes.png"
    pic.write_bytes(PNG)
    txt.write_text("not a picture")
    card = {"type": "needs_you", "title": "t", "headline": "h", "needs": {"kind": "decision", "replies": [
        {"key": "A", "label": "a", "reply": "a", "screens": [{"label": "s", "url": "/static/scratch/x.html#a",
                                                             "image": {"phone": str(pic), "web": str(txt)}}]}]}}
    assert cp.save_card(card) == 1


def test_files_outside_home_and_temp_are_never_copied(store):
    assert cp.local_file("/etc/passwd") is None
    assert cp.local_file("/static/../daemon/handoff_rules.py") is None


def test_saving_is_off_in_the_suite_by_default(store, monkeypatch):
    monkeypatch.setenv("VIBENODE_CARD_PICTURES", "0")
    shot = store / "shot.png"
    shot.write_bytes(PNG)
    assert cp.save_card(_card(str(shot))) == 0


def test_a_passing_stop_saves_the_pictures(store, monkeypatch):
    from engine import handoff_rules, smart_termination
    shot = store / "stop.png"
    shot.write_bytes(PNG)
    monkeypatch.setattr(smart_termination, "_review", lambda *a, **k: None)
    monkeypatch.setattr(handoff_rules, "extract", lambda m: (_card(str(shot)), "", None))
    smart_termination.review("```handoff\n{}\n```", continued=False, sendbacks=0)
    shot.unlink()
    assert cp.find(str(shot)) is not None


def test_the_route_serves_the_copy(store):
    from web import create_app
    shot = store / "route.png"
    shot.write_bytes(PNG)
    card = _card(str(shot))
    cp.save_card(card)
    shot.unlink()
    client = create_app(testing=True).test_client()
    r = client.get("/api/card-picture", query_string={"u": str(shot), "t": card["title"], "h": card["headline"]})
    assert r.status_code == 200 and r.data == PNG and r.mimetype == "image/png"
    assert client.get("/api/card-picture", query_string={"u": "/static/scratch/never.png"}).status_code == 404


def test_the_card_falls_back_to_the_copy_when_a_picture_fails():
    js = (ROOT / "src" / "web" / "static" / "js" / "last-card.js").read_text(encoding="utf-8")
    assert "/api/card-picture?u=" in js and "document.addEventListener('error'" in js
    assert "img.dataset.vnSaved" in js   # one try per picture, never a loop
