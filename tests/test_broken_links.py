"""A summary's pictures and pages on this app must load (2026-10-30: two
pictures linked as /api/preview/asset/<upload name> opened to nothing on the
owner's iPhone).  Smart termination checks this app's own addresses against
the files that serve them and sends back any that are missing."""
import json
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from daemon import handoff_rules as hr  # noqa: E402


@pytest.fixture(autouse=True)
def _check_on(monkeypatch):
    monkeypatch.setenv("VIBENODE_LINK_CHECK", "1")


@pytest.fixture
def real_files(tmp_path, monkeypatch):
    static, previews = tmp_path / "static", tmp_path / "previews"
    (static / "scratch").mkdir(parents=True)
    previews.mkdir()
    (static / "scratch" / "today.png").write_bytes(b"png")
    (static / "scratch" / "page.html").write_text("<p>x</p>", encoding="utf-8")
    pid = uuid.uuid4().hex
    (previews / (pid + ".png")).write_bytes(b"png")
    monkeypatch.setattr(hr, "_STATIC_DIR", static)
    monkeypatch.setattr(hr, "_PREVIEW_DIR", previews)
    return pid


def _card(**extra):
    c = {"type": "explain", "title": "Picture links", "headline": "x", "answer": "y"}
    c.update(extra)
    return c


def test_the_owners_case_is_sent_back(real_files):
    card = _card(links=[{"label": "Today", "kind": "image", "url": "/api/preview/asset/20261030-111644-image.png"}])
    probs = hr.broken_link_problems(card)
    assert probs and "20261030-111644-image.png" in probs[0] and "static/scratch" in probs[0]
    assert "open to nothing" in (hr.review("```handoff\n" + json.dumps(card) + "\n```") or "")


def test_real_pictures_and_pages_pass(real_files):
    card = _card(links=[{"label": "a", "kind": "image", "url": "/static/scratch/today.png"},
                        {"label": "b", "kind": "image", "url": "/api/preview/asset/" + real_files}],
                 validate={"how": "open", "url": "/static/scratch/page.html?theme=dark#a"},
                 needs={"kind": "none", "look": [{"label": "p", "kind": "link",
                                                  "url": "http://127.0.0.1:5050/static/scratch/page.html"}]})
    assert hr.broken_link_problems(card) == []


@pytest.mark.parametrize("url", ["/static/scratch/missing.png", "/static/scratch/missing.html#a",
                                 "/api/preview/asset/" + "0" * 32, "/static/../daemon/handoff_rules.py",
                                 "http://localhost:5050/static/scratch/gone.png"])
def test_missing_or_escaping_addresses_are_sent_back(real_files, url):
    assert hr.broken_link_problems(_card(links=[{"label": "x", "kind": "image", "url": url}]))


def test_screens_and_files_are_checked_too(real_files):
    card = _card(needs={"kind": "decision", "replies": [{"key": "A", "label": "a", "reply": "a",
                                                         "screens": [{"label": "s", "url": "/static/scratch/nope.html#a"}]}]},
                 deliverable={"files": [{"label": "zip", "url": "/static/scratch/nope.zip"}]})
    p = hr.broken_link_problems(card)[0]
    assert "screens[0]" in p and "files[0]" in p


def test_other_sites_and_other_routes_are_not_checked(real_files):
    card = _card(links=[{"label": "x", "kind": "link", "url": "https://example.com/a.png"},
                        {"label": "y", "kind": "link", "url": "/?project=x"},
                        {"label": "z", "kind": "link", "url": "http://127.0.0.1:5173/"}])
    assert hr.broken_link_problems(card) == []


def test_it_can_be_turned_off(real_files, monkeypatch):
    monkeypatch.setenv("VIBENODE_LINK_CHECK", "0")
    card = _card(links=[{"label": "x", "kind": "image", "url": "/static/scratch/missing.png"}])
    assert hr.broken_link_problems(card) == []
