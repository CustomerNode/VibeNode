"""
Tests for the subsession inbox storage module (spec §8 + §4.3).

Covers:
  - Schema round-trip
  - Atomic write (the temp file disappears, only inbox.json remains)
  - Drain ordering (FIFO of undelivered)
  - 100-entry cap with delivered-first eviction (spec §7.3)
  - Concurrent write (two children reporting at once)
  - Missing-inbox tolerance (load_inbox returns empty, no raise)
  - Corrupted-inbox tolerance (renamed to .broken-<ts>, empty returned)

The autouse ``_isolate_daemon_home`` fixture in conftest.py points
``Path.home()`` at a tmp dir, so writes land under tmp/.claude/
vibenode-state/ and never pollute the user's real home.
"""

import json
import threading
import time
from pathlib import Path

import pytest

from daemon import subsession_inbox as ibx


# ---------------------------------------------------------------------------
# Schema round-trip
# ---------------------------------------------------------------------------

class TestSchemaRoundTrip:
    def test_load_empty_inbox_returns_default_shape(self):
        """A missing inbox file produces the canonical empty shape."""
        sid = "parent-empty"
        data = ibx.load_inbox(sid)
        assert data == {"version": 1, "pending_reports": []}

    def test_append_then_load_round_trip(self):
        """The entry we wrote shows up byte-for-byte (modulo serialization)
        on the next load."""
        sid = "parent-rt"
        entry = ibx.append_report(
            parent_sid=sid,
            child_sid="child-x",
            child_name="Investigate fork-rewind edge case",
            summary="Found a one-liner fix at line 882",
            attachments=[{"type": "file_ref", "path": "x.py", "line": 882}],
        )
        data = ibx.load_inbox(sid)
        assert data["version"] == 1
        assert len(data["pending_reports"]) == 1
        loaded = data["pending_reports"][0]
        assert loaded["report_id"] == entry["report_id"]
        assert loaded["child_session_id"] == "child-x"
        assert loaded["summary"] == "Found a one-liner fix at line 882"
        assert loaded["delivered"] is False
        assert loaded["attachments"][0]["line"] == 882
        # reported_at is ISO8601 with a Z suffix.
        assert loaded["reported_at"].endswith("Z")


# ---------------------------------------------------------------------------
# Atomic write
# ---------------------------------------------------------------------------

class TestAtomicWrite:
    def test_no_tmp_file_left_after_write(self):
        """After a successful append, only inbox.json exists in the
        parent's vibenode-state directory — no leftover .tmp files
        from the tempfile + os.replace dance."""
        sid = "parent-atomic"
        ibx.append_report(
            parent_sid=sid,
            child_sid="child-1",
            child_name="atom",
            summary="ok",
        )
        d = ibx.inbox_dir_for(sid)
        assert d.is_dir()
        leftover = [
            p.name for p in d.iterdir() if p.name != "inbox.json"
        ]
        assert leftover == [], f"Stray temp files: {leftover}"

    def test_write_lazy_creates_directory(self):
        """The parent vibenode-state/<sid>/ directory is created on the
        first write — we do NOT pre-create it at session start (spec §4.3.3)."""
        sid = "parent-lazy"
        assert not ibx.inbox_dir_for(sid).exists()
        ibx.append_report(
            parent_sid=sid,
            child_sid="c",
            child_name="lazy",
            summary="first",
        )
        assert ibx.inbox_dir_for(sid).is_dir()


# ---------------------------------------------------------------------------
# Drain ordering (FIFO of undelivered)
# ---------------------------------------------------------------------------

class TestDrainOrdering:
    def test_drain_returns_undelivered_in_fifo_order(self):
        sid = "parent-fifo"
        for i in range(3):
            ibx.append_report(
                parent_sid=sid,
                child_sid=f"child-{i}",
                child_name=f"c{i}",
                summary=f"summary-{i}",
            )
        drained = ibx.drain_undelivered(sid)
        assert [r["summary"] for r in drained] == [
            "summary-0", "summary-1", "summary-2",
        ]

    def test_drain_marks_entries_delivered_on_disk(self):
        sid = "parent-mark"
        ibx.append_report(sid, "c", "child", "first")
        ibx.drain_undelivered(sid)
        data = ibx.load_inbox(sid)
        assert all(r["delivered"] for r in data["pending_reports"])

    def test_drain_skips_already_delivered(self):
        sid = "parent-mixed"
        ibx.append_report(sid, "c1", "child1", "first")
        ibx.drain_undelivered(sid)
        # Second report arrives after the drain.
        ibx.append_report(sid, "c2", "child2", "second")
        drained2 = ibx.drain_undelivered(sid)
        assert len(drained2) == 1
        assert drained2[0]["summary"] == "second"

    def test_drain_returns_empty_when_no_undelivered(self):
        sid = "parent-empty-drain"
        assert ibx.drain_undelivered(sid) == []
        # Even after appending + delivering, a second drain returns [].
        ibx.append_report(sid, "c", "child", "x")
        ibx.drain_undelivered(sid)
        assert ibx.drain_undelivered(sid) == []


# ---------------------------------------------------------------------------
# 100-entry cap with delivered-first eviction
# ---------------------------------------------------------------------------

class TestCap:
    def test_cap_does_not_kick_in_under_threshold(self):
        sid = "parent-undercap"
        for i in range(ibx.MAX_PENDING_REPORTS):
            ibx.append_report(sid, f"c{i}", "name", f"s{i}")
        data = ibx.load_inbox(sid)
        assert len(data["pending_reports"]) == ibx.MAX_PENDING_REPORTS

    def test_cap_evicts_delivered_first(self):
        """When the inbox is at the cap and a new undelivered report
        arrives, the oldest delivered entry is evicted before any
        undelivered one (spec §7.3)."""
        sid = "parent-cap-delivered"
        # 50 reports, drained (now delivered).
        for i in range(50):
            ibx.append_report(sid, f"c{i}", "old", f"old-{i}")
        ibx.drain_undelivered(sid)
        # 50 more reports, still undelivered.
        for i in range(50):
            ibx.append_report(sid, f"d{i}", "new", f"new-{i}")
        data = ibx.load_inbox(sid)
        assert len(data["pending_reports"]) == 100

        # 101st write — must evict the oldest delivered entry, not new ones.
        ibx.append_report(sid, "fresh", "fresh", "fresh-msg")
        data = ibx.load_inbox(sid)
        assert len(data["pending_reports"]) == 100
        # The oldest delivered ("old-0") should be gone.
        summaries = [r["summary"] for r in data["pending_reports"]]
        assert "old-0" not in summaries
        # All 50 undelivered remain plus the fresh one.
        assert "new-49" in summaries
        assert "fresh-msg" in summaries

    def test_cap_evicts_undelivered_when_no_delivered_to_drop(self):
        """When the cap is hit and every entry is undelivered, the
        oldest undelivered is evicted (anti-DoS for a misbehaving
        child)."""
        sid = "parent-cap-all-undelivered"
        for i in range(101):
            ibx.append_report(sid, f"c{i}", "spam", f"s{i}")
        data = ibx.load_inbox(sid)
        assert len(data["pending_reports"]) == 100
        # s0 (oldest) should be gone, s100 (newest) remains.
        summaries = [r["summary"] for r in data["pending_reports"]]
        assert "s0" not in summaries
        assert "s100" in summaries


# ---------------------------------------------------------------------------
# Concurrent write (two children reporting at once)
# ---------------------------------------------------------------------------

class TestConcurrency:
    def test_two_children_reporting_in_parallel_lose_nothing(self):
        """Spawn 10 threads each appending 10 reports — none of the
        100 writes is lost, and the on-disk count matches the sum."""
        sid = "parent-concurrent"
        per_thread = 10
        nthreads = 10

        def _worker(thread_idx):
            for i in range(per_thread):
                ibx.append_report(
                    sid,
                    f"child-{thread_idx}",
                    f"c{thread_idx}",
                    f"t{thread_idx}-i{i}",
                )

        threads = [
            threading.Thread(target=_worker, args=(t,))
            for t in range(nthreads)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        data = ibx.load_inbox(sid)
        assert len(data["pending_reports"]) == per_thread * nthreads
        # Each report has a unique report_id.
        ids = {r["report_id"] for r in data["pending_reports"]}
        assert len(ids) == per_thread * nthreads


# ---------------------------------------------------------------------------
# Missing-inbox + corruption tolerance (spec §9)
# ---------------------------------------------------------------------------

class TestErrorHandling:
    def test_load_missing_inbox_does_not_raise(self):
        # Just calling load_inbox on a never-seen SID returns empty.
        data = ibx.load_inbox("never-existed-sid")
        assert data["pending_reports"] == []
        # And does NOT create any files.
        assert not ibx.inbox_dir_for("never-existed-sid").exists()

    def test_corrupted_inbox_renamed_and_treated_as_empty(self):
        sid = "parent-corrupt"
        path = ibx.inbox_path_for(sid)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("this is not JSON {{{ broken", encoding="utf-8")

        # load_inbox must NOT raise.
        data = ibx.load_inbox(sid)
        assert data["pending_reports"] == []
        # The broken file is renamed sideways.
        broken_files = list(path.parent.glob("inbox.json.broken-*"))
        assert len(broken_files) == 1


# ---------------------------------------------------------------------------
# has_undelivered + undelivered_count helpers
# ---------------------------------------------------------------------------

class TestQueries:
    def test_has_undelivered_reflects_disk_state(self):
        sid = "parent-flag"
        assert ibx.has_undelivered(sid) is False
        ibx.append_report(sid, "c", "child", "msg")
        assert ibx.has_undelivered(sid) is True
        ibx.drain_undelivered(sid)
        assert ibx.has_undelivered(sid) is False

    def test_undelivered_count_is_accurate(self):
        sid = "parent-count"
        assert ibx.undelivered_count(sid) == 0
        ibx.append_report(sid, "c1", "child1", "a")
        ibx.append_report(sid, "c2", "child2", "b")
        assert ibx.undelivered_count(sid) == 2
        ibx.drain_undelivered(sid)
        assert ibx.undelivered_count(sid) == 0


# ---------------------------------------------------------------------------
# Drain block formatting
# ---------------------------------------------------------------------------

class TestFormatDrainBlock:
    def test_empty_block_for_empty_entries(self):
        assert ibx.format_drain_block([]) == ""

    def test_block_has_header_and_each_report(self):
        sid = "parent-format"
        ibx.append_report(
            sid, "child-abcdef12-3456",
            "Investigate edge case",
            "Found a one-liner fix",
        )
        drained = ibx.drain_undelivered(sid)
        block = ibx.format_drain_block(drained)
        assert block.startswith(
            "[Subsession reports — surfaced before your next message]"
        )
        assert "Investigate edge case" in block
        assert "Found a one-liner fix" in block
        # Short SID is the first 8 chars.
        assert "child-ab" in block

    def test_block_renders_file_ref_attachments(self):
        """attachments[] of type file_ref render as path:line under the
        report so the parent can act on them (spec §4.3.2 / §6.7)."""
        sid = "parent-attach"
        ibx.append_report(
            sid, "child-z", "Locate the slicer bug",
            "The slicer drops the trailing partial line",
            attachments=[
                {"type": "file_ref", "path": "app/routes/sessions_api.py", "line": 882},
                {"type": "file_ref", "path": "README.md"},  # no line
            ],
        )
        drained = ibx.drain_undelivered(sid)
        block = ibx.format_drain_block(drained)
        assert "app/routes/sessions_api.py:882" in block
        assert "README.md" in block
        # The no-line ref must not render a dangling colon.
        assert "README.md:" not in block

    def test_block_omits_refs_line_when_no_attachments(self):
        sid = "parent-noattach"
        ibx.append_report(sid, "child-q", "Plain report", "Just text")
        drained = ibx.drain_undelivered(sid)
        block = ibx.format_drain_block(drained)
        assert "refs:" not in block

    def test_causal_chain_id_round_trips_when_provided(self):
        """The spawn-time lineage UUID is persisted on the entry when
        provided and absent otherwise (Patent 04/06 chain-of-custody)."""
        sid = "parent-chain"
        entry = ibx.append_report(
            sid, "child-c", "child", "msg",
            causal_chain_id="chain-1234",
        )
        assert entry["causal_chain_id"] == "chain-1234"
        loaded = ibx.load_inbox(sid)["pending_reports"][0]
        assert loaded["causal_chain_id"] == "chain-1234"

    def test_causal_chain_id_absent_when_not_provided(self):
        sid = "parent-nochain"
        entry = ibx.append_report(sid, "child-c", "child", "msg")
        assert "causal_chain_id" not in entry


# ---------------------------------------------------------------------------
# remove_inbox
# ---------------------------------------------------------------------------

class TestRemoveInbox:
    def test_remove_inbox_purges_the_directory(self):
        sid = "parent-purge"
        ibx.append_report(sid, "c", "child", "msg")
        assert ibx.inbox_dir_for(sid).is_dir()
        ibx.remove_inbox(sid)
        assert not ibx.inbox_dir_for(sid).exists()

    def test_remove_inbox_tolerates_missing_directory(self):
        # No raise even when there's nothing to remove.
        ibx.remove_inbox("never-existed")
