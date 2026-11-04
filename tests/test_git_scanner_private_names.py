"""Private-document filename patterns: short acronyms must stand alone.

"nda" as a bare substring blocked src/shared/playbook_standard.py ("sta-nda-rd")
from publishing. The acronyms now need non-letters on both sides.
"""
from web.git_scanner import _PRIVATE_DOC_PATTERNS


def _flagged(name):
    return any(p.search(name) for p in _PRIVATE_DOC_PATTERNS)


def test_words_containing_acronyms_pass():
    for name in ("playbook_standard.py", "test_playbook_standard.py",
                 "mouse.js", "loiter.py", "classnames.ts"):
        assert not _flagged(name), name


def test_real_private_names_still_block():
    for name in ("nda.pdf", "client_nda.md", "Acme-NDA.txt", "loi_acme.txt",
                 "mou.md", "ssn.txt", "invoice_2026.html"):
        assert _flagged(name), name
