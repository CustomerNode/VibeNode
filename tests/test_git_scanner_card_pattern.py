"""GUARD for the "Credit Card" rule in ``app/git_scanner.py``.

This rule has failed in both directions, and both are expensive:

1. **Too broad.** The original pattern was 16 digits behind an issuer prefix,
   nothing more. Every hex UUID in the codebase whose 4th group starts with
   4, 5, 3 or 6 -- e.g. ``beef1111-2222-3333-4444-555566667777`` -- contains
   ``4444-555566667777`` and tripped it. That blocked the pre-push scan on a
   plain test fixture, and a scanner that cries wolf on ordinary source is a
   scanner people learn to click past.
2. **Too narrow.** Requiring exactly 16 digits meant a 15-digit Amex written
   in its natural 4-6-5 grouping was invisible. A real card could have been
   pushed to a public repo while the scan reported "all clear."

The rule now has two layers: lookarounds that keep it off ``<hex>-`` bounded
UUID groups, and a Luhn checksum in ``_MATCH_VALIDATORS``. Luhn carries the
accuracy, which is what lets the regex stay broad enough to catch Amex.

**When you touch that pattern or its validator, add a case here** -- one
proving a real card shape is still caught, one proving the lookalike is not.

Card numbers below are the publicly published test values (they are Luhn-valid
but were never issued to anyone) and they are assembled from fragments at run
time on purpose: writing them as literals would put a card-shaped digit run
into this repository and trip the very scanner under test.

Run: python -m pytest tests/test_git_scanner_card_pattern.py -q
Static only: matches strings and scans a throwaway repo under tmp_path.
"""
from __future__ import annotations

import json
import subprocess

import pytest

from app.git_scanner import (
    _MATCH_VALIDATORS,
    _SECRET_PATTERNS,
    _luhn_ok,
    scan_staged_files,
    scan_staged_files_stream,
)

CARD_LABEL = "Credit Card"


def _rule():
    for label, pattern, _desc in _SECRET_PATTERNS:
        if label == CARD_LABEL:
            return pattern
    pytest.fail(f"{CARD_LABEL!r} rule missing from _SECRET_PATTERNS")


def _flagged(text: str) -> bool:
    """Mirror the scanner's decision for one rule: regex match AND validator."""
    validator = _MATCH_VALIDATORS.get(CARD_LABEL)
    for match in _rule().finditer(text):
        if validator is None or validator(match.group(0)):
            return True
    return False


# Assembled at run time -- see module docstring.
VISA = "4111" + "1111" * 3
VISA_DASHED = "-".join(["4111", "1111", "1111", "1111"])
MASTERCARD = " ".join(["5500", "0000", "0000", "0004"])
DISCOVER = "6011" + "1111" * 2 + "1117"
AMEX = "3782" + "822463" + "10005"
AMEX_SPACED = " ".join(["3782", "822463", "10005"])
AMEX_DASHED = "-".join(["3782", "822463", "10005"])

# MUST be caught. A miss here means a real card can reach a public repo.
MUST_FLAG = [
    VISA,
    VISA_DASHED,
    MASTERCARD,
    DISCOVER,
    AMEX,          # 15 digits -- invisible to the old 16-digit-only pattern
    AMEX_SPACED,
    AMEX_DASHED,
    f"card = '{VISA_DASHED}'",
    f"CARD_ON_FILE = {AMEX_SPACED}",
]

# MUST stay quiet. A hit here blocks a push over ordinary source.
MUST_NOT_FLAG = [
    'sid = "beef1111-2222-3333-4444-555566667777"',   # the original false positive
    'sid = "cafe1111-2222-3333-4444-555566667777"',   # ditto
    "12345678-1234-5678-5555-666677778888",           # 4th group 5xxx
    "12345678-1234-5678-3712-345612345678",           # 4th group 3xxx (Amex-shaped)
    "4111" + "1111" * 2 + "1112",                     # card-shaped, Luhn-broken
    "4444" + "5555" + "6666" + "7777",                # arbitrary digit run
    "timestamp 1700000000 seq 4444 5555 6666",        # too few digits
]


@pytest.mark.parametrize("text", MUST_FLAG)
def test_real_card_shapes_are_flagged(text):
    assert _flagged(text), f"card number not detected: {text!r}"


@pytest.mark.parametrize("text", MUST_NOT_FLAG)
def test_lookalikes_are_not_flagged(text):
    assert not _flagged(text), f"false positive on: {text!r}"


def test_luhn_validator_is_wired_to_the_card_rule():
    """The regex alone must not decide. Without the validator the Luhn-broken
    run below is reported, which is exactly the bug class this guards."""
    broken = "4111" + "1111" * 2 + "1112"
    assert _rule().search(broken), "sample no longer exercises the validator"
    assert _MATCH_VALIDATORS.get(CARD_LABEL) is _luhn_ok
    assert not _flagged(broken)


def test_luhn_rejects_lengths_outside_card_range():
    assert not _luhn_ok("0" * 12)   # too short
    assert not _luhn_ok("0" * 20)   # too long
    assert _luhn_ok(VISA)


# ── End-to-end: prove the validator is wired into BOTH scan entry points ──
# scan_staged_files and scan_staged_files_stream duplicate the match loop, so a
# fix applied to one and not the other looks correct in unit tests and still
# blocks the push through the other code path.

def _tmp_repo(tmp_path, body: str):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "sample_data.py").write_text(body, encoding="utf-8")
    return tmp_path


def _card_findings(result: dict):
    return [f for f in result["findings"] if f["type"] == CARD_LABEL]


def test_scan_reports_a_real_card(tmp_path):
    proj = _tmp_repo(tmp_path, f'CARD = "{VISA_DASHED}"\n')
    assert _card_findings(scan_staged_files(proj)), "real card was not reported"


def test_scan_stays_clear_on_uuid_fixtures(tmp_path):
    proj = _tmp_repo(tmp_path, 'SID = "beef1111-2222-3333-4444-555566667777"\n')
    assert _card_findings(scan_staged_files(proj)) == []


def test_stream_scan_matches_the_plain_scan(tmp_path):
    proj = _tmp_repo(
        tmp_path,
        'SID = "beef1111-2222-3333-4444-555566667777"\n'
        f'CARD = "{AMEX_SPACED}"\n',
    )
    done = json.loads(list(scan_staged_files_stream(proj))[-1])
    assert done["type"] == "done"
    hits = [f for f in done["findings"] if f["type"] == CARD_LABEL]
    assert len(hits) == 1, f"expected only the Amex line, got {hits}"
    assert hits[0]["line"] == 2
