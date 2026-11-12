"""A Controlled A/B Test trial folder a killed worker left locked is unlocked
by the next run, so pytest can delete it (tests/conftest.py,
_unlock_leftover_trial_folders).  Real folders, the real lock."""

import os
import shutil
import time

from features.experiments import hideaway
from tests import conftest as cf


def _leftover(root, name="mul-test-20261102-232801-7aa5ae", age=3600):
    d = root / "garbage-1234" / "popen-gw2" / "test_watch_listing_the_parent0" / "work" / name
    (d / "0123456789abcdef").mkdir(parents=True)
    assert hideaway.hide(d)["ok"]
    old = time.time() - age
    os.utime(d, (old, old))
    return d


def test_a_locked_leftover_is_unlocked_and_can_then_be_deleted(tmp_path):
    d = _leftover(tmp_path)
    try:
        assert not hideaway.listable(d)
        assert cf._unlock_leftover_trial_folders(tmp_path) == 1
        assert hideaway.listable(d)
        shutil.rmtree(tmp_path / "garbage-1234")          # what pytest's own cleanup does
        assert not (tmp_path / "garbage-1234").exists()
    finally:
        if d.exists():
            hideaway.unhide(d)


def test_a_folder_a_running_test_just_locked_is_left_alone(tmp_path):
    d = _leftover(tmp_path, age=0)
    try:
        assert cf._unlock_leftover_trial_folders(tmp_path) == 0
        assert not hideaway.listable(d)
    finally:
        hideaway.unhide(d)


def test_only_trial_folders_are_touched(tmp_path):
    d = _leftover(tmp_path, name="something-else")
    try:
        assert cf._unlock_leftover_trial_folders(tmp_path) == 0
        assert not hideaway.listable(d)
    finally:
        hideaway.unhide(d)


def test_a_missing_root_is_nothing_to_do(tmp_path):
    assert cf._unlock_leftover_trial_folders(tmp_path / "nope") == 0
