"""Queued messages handled since the user last looked (owner's pick C with the
history-clock icon, 2026-10-28).  The web server records when a message is
queued, when the engine sends it, and when he sends something himself."""

import subprocess
from pathlib import Path

import pytest

from app import queue_history as qh

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def tmp_store(tmp_path):
    qh._reset_for_tests(tmp_path / "queue_history.json")
    yield
    qh._reset_for_tests(qh.PATH)


def test_queued_then_sent_shows_until_he_sends_himself():
    qh.note_queued("s1", "also make the tab glide")
    qh.note_queued("s1", "check it on the phone")
    qh.note_dispatched("s1", "also make the tab glide")
    qh.note_dispatched("s1", "check it on the phone")
    got = qh.since_review("s1")["items"]
    assert [i["text"] for i in got] == ["also make the tab glide", "check it on the phone"]
    assert all(i["queued_at"] and i["sent_at"] >= i["queued_at"] for i in got)
    qh.note_direct("s1")
    assert qh.since_review("s1")["items"] == [] and qh.since_review("s1")["reviewed_at"] > 0


def test_nothing_queued_means_nothing_to_show():
    qh.note_direct("s2")
    assert qh.since_review("s2")["items"] == []
    assert qh.since_review("never-seen") == {"reviewed_at": 0, "items": []}


def test_an_edited_queue_item_still_counts_without_its_queue_time():
    qh.note_queued("s3", "first wording")
    qh.note_dispatched("s3", "edited wording")
    (i,) = qh.since_review("s3")["items"]
    assert i["text"] == "edited wording" and i["queued_at"] is None


def test_it_survives_a_restart_and_follows_a_remap():
    qh.note_queued("tmp-1", "x y z")
    qh.note_dispatched("tmp-1", "x y z")
    path = qh.PATH
    qh._reset_for_tests(path)            # a web restart: read back from disk
    qh.note_remap("tmp-1", "real-1")
    assert [i["text"] for i in qh.since_review("real-1")["items"]] == ["x y z"]
    assert qh.since_review("tmp-1")["items"] == []


def test_the_file_is_never_published():
    r = subprocess.run(["git", "check-ignore", "-q", "data/queue_history.json"], cwd=ROOT)
    assert r.returncode == 0


def test_the_endpoint_serves_it():
    from app import create_app
    qh.note_queued("s9", "hello there friend")
    qh.note_dispatched("s9", "hello there friend")
    client = create_app(testing=True).test_client()
    r = client.get("/api/queue-history/s9")
    assert r.status_code == 200
    assert [i["text"] for i in r.get_json()["items"]] == ["hello there friend"]
