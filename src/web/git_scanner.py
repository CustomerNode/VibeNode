"""
Deterministic secret & sensitive-data scanner for pre-push validation.

Scans ALL tracked + untracked files for secrets, API keys, tokens, passwords,
credentials, and PII before allowing a git push. Fully deterministic —
no AI involved, just regex pattern matching.
"""

import functools
import hashlib
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable, Dict

from .config import _VIBENODE_DIR

from .platform_utils import NO_WINDOW as _NO_WINDOW

# ── Secret patterns (compiled regexes) ──
# Each entry: (label, regex, description)
_SECRET_PATTERNS = [
    # ── Cloud provider keys ──
    ("AWS Access Key", re.compile(r'AKIA[0-9A-Z]{16}'), "AWS access key ID"),
    ("AWS Secret Key", re.compile(r'(?i)(aws[_-]?secret[_-]?access[_-]?key|aws[_-]?secret)\s*[=:]\s*["\']?[A-Za-z0-9/+=]{40}'), "AWS secret access key"),
    ("Google API Key", re.compile(r'AIza[A-Za-z0-9_\-]{35}'), "Google API key"),
    ("DigitalOcean Token", re.compile(r'do[po]_v1_[a-f0-9]{64}'), "DigitalOcean token"),

    # ── Git/code platform tokens ──
    ("GitHub Token", re.compile(r'gh[pousr]_[A-Za-z0-9_]{36,255}'), "GitHub personal access token"),
    ("GitHub OAuth", re.compile(r'gho_[A-Za-z0-9]{36,255}'), "GitHub OAuth token"),
    ("npm Token", re.compile(r'npm_[A-Za-z0-9]{36}'), "npm access token"),
    ("PyPI Token", re.compile(r'pypi-[A-Za-z0-9_\-]{50,}'), "PyPI API token"),

    # ── AI provider keys ──
    ("OpenAI Key", re.compile(r'sk-(?!ant-)[A-Za-z0-9_\-]{20,}'), "OpenAI API key"),
    ("Anthropic Key", re.compile(r'sk-ant-[A-Za-z0-9_\-]{20,}'), "Anthropic API key"),

    # ── Communication/SaaS tokens ──
    ("Slack Token", re.compile(r'xox[bporas]-[0-9]{10,13}-[A-Za-z0-9\-]{10,}'), "Slack token"),
    ("Discord Bot Token", re.compile(r'[MN][A-Za-z0-9]{23,}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{27,}'), "Discord bot token"),
    ("Twilio API Key", re.compile(r'SK[0-9a-fA-F]{32}'), "Twilio API key"),
    ("SendGrid API Key", re.compile(r'SG\.[A-Za-z0-9_\-]{22}\.[A-Za-z0-9_\-]{43}'), "SendGrid API key"),
    ("Mailgun API Key", re.compile(r'key-[0-9a-zA-Z]{32}'), "Mailgun API key"),

    # ── Payment/billing keys ──
    ("Stripe Key", re.compile(r'[sr]k_(live|test)_[A-Za-z0-9]{20,}'), "Stripe API key"),

    # ── Database/infrastructure ──
    ("Supabase Key", re.compile(r'(?i)(supabase[_-]?key|supabase[_-]?url|anon[_-]?key|service[_-]?role[_-]?key)\s*[=:]\s*["\']?[A-Za-z0-9._\-]{20,}'), "Supabase credential"),
    ("Database URL", re.compile(r'(?i)(database[_-]?url|db[_-]?url|mongo[_-]?uri|postgres[_-]?url|mysql[_-]?url)\s*[=:]\s*["\']?[a-z]+://[^\s"\']{10,}'), "Database connection string"),
    ("Connection String", re.compile(r'(?i)(mongodb|postgres|mysql|redis|amqp)://[^:]+:[^@]+@'), "Connection string with embedded credentials"),

    # ── Generic assignment patterns ──
    ("Generic API Key", re.compile(r'(?i)(api[_-]?key|apikey)\s*[=:]\s*["\']?[A-Za-z0-9_\-]{20,}'), "Generic API key assignment"),
    ("Generic Secret", re.compile(r'(?i)(secret[_-]?key|client[_-]?secret|app[_-]?secret)\s*[=:]\s*["\']?[A-Za-z0-9_\-]{16,}'), "Generic secret assignment"),
    ("Generic Token", re.compile(r'(?i)(access[_-]?token|auth[_-]?token|bearer[_-]?token)\s*[=:]\s*["\']?[A-Za-z0-9_\-\.]{20,}'), "Token assignment"),
    ("Bearer Token", re.compile(r'(?i)bearer\s+[A-Za-z0-9_\-\.]{20,}'), "Bearer auth token in header"),
    # The value class excludes newlines. A negated class like [^"'] happily
    # matches \n, so on a line such as  body: 'password=' + encode(pw),  the
    # regex read the opening quote and slurped the following lines of code
    # until it found the next quote — reporting a multi-line JS blob as a
    # "hardcoded password". A real password literal never spans newlines, so
    # anchoring the class costs no detection and kills that whole FP class.
    ("Password Assignment", re.compile(r'(?i)(password|passwd|pwd)\s*[=:]\s*["\'][^"\'\r\n]{8,}["\']'), "Hardcoded password"),

    # ── Cryptographic material ──
    ("Private Key", re.compile(r'-----BEGIN (RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----'), "Private key file"),
    ("JWT Token", re.compile(r'eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}'), "Hardcoded JWT"),

    # ── Encoded secrets ──
    ("Base64 Secret", re.compile(r'(?i)(secret|key|token|password|credential|auth)\s*[=:]\s*["\']?[A-Za-z0-9+/]{40,}={0,2}["\']?'), "Possible base64-encoded secret"),

    # ── Environment variable patterns ──
    # The negative lookahead excludes values that are a quoted pure-lowercase
    # snake_case identifier (e.g. _CONFIG_KEY = "mobile_command_enabled"). Those
    # are config-dict key constants, not secrets — real credentials always carry
    # digits, mixed case, or special chars, so this never suppresses a true secret.
    # The second and third lookaheads exclude values that are Python code, not
    # a literal: a container opened on the line ( ( [ { ) or a call such as
    # frozenset(...), re.compile(...), os.environ.get(...). Constants like
    # ZOOM_KEYS = ((...)) and KNOWN_KEYS = frozenset({...}) used to need a
    # per-file suppression, which also blinded those files to a REAL generic
    # secret (SERVICE_TOKEN = "Zq81LmPx4Rt" went unreported). Known secret
    # shapes (sk-, ghp_, AKIA...) are caught by their own patterns either way.
    # Do NOT go back to per-file "Env Variable Secret" suppressions.
    ("Env Variable Secret", re.compile(r'(?i)^[A-Z_]*(SECRET|TOKEN|KEY|PASSWORD|CREDENTIAL|AUTH)[A-Z_]*\s*=\s*(?!["\'][a-z]+(?:_[a-z]+)*["\']\s*$)(?![(\[{])(?![A-Za-z_][\w.]*\()[^\s]{8,}', re.MULTILINE), "Environment variable with secret value"),

    # ── PII / personal data ──
    ("Hardcoded User Path", re.compile(r'(?i)["\'][A-Z]:\\\\Users\\\\[a-zA-Z][a-zA-Z0-9._\-]{2,}\\\\|["\']/(?:Users|home)/[a-zA-Z][a-zA-Z0-9._\-]{2,}/'), "Hardcoded personal filesystem path"),

    # ── Internal network ──
    ("Internal IP URL", re.compile(r'https?://(?:192\.168\.|10\.|172\.(?:1[6-9]|2[0-9]|3[01])\.)\d+'), "Internal/private network URL"),
    ("Internal Hostname", re.compile(r'https?://[a-z][a-z0-9\-]+\.(internal|local|corp|lan|home|intranet)\b'), "Internal/corporate hostname"),

    # ── Sensitive document content ──
    ("Confidential Marker", re.compile(r'(?i)\b(CONFIDENTIAL|DO NOT DISTRIBUTE|INTERNAL USE ONLY|TRADE SECRET|ATTORNEY[\s\-]CLIENT PRIVILEGED|NOT FOR PUBLIC RELEASE|STRICTLY PRIVATE)\b'), "Document contains confidentiality marker"),
    ("SSN Pattern", re.compile(r'\b\d{3}-\d{2}-\d{4}\b'), "Possible Social Security Number"),
    # Two layers keep this off UUIDs/GUIDs, which are everywhere in this
    # codebase (session ids, test fixtures). A UUID like
    # "beef1111-2222-3333-4444-555566667777" contains "4444-555566667777",
    # which is 16 digits with an issuer-shaped "4xxx" prefix — a textbook
    # false positive.
    #   1. The lookarounds: real card numbers are not preceded by "<hex>-"
    #      nor followed by "-<hex>"; UUID groups always are.
    #   2. The Luhn check in _MATCH_VALIDATORS below: every real card number
    #      passes the Luhn checksum, arbitrary digit runs almost never do.
    # Do NOT drop either guard to "simplify" the pattern — it re-breaks the
    # pre-push scan on any file containing a hex UUID whose 4th group starts
    # with 4, 5, 3 or 6. Because Luhn carries the accuracy, the pattern itself
    # can afford to be broad: the first branch covers 15-digit Amex in its
    # natural 4-6-5 grouping, which a 16-digit-only pattern missed entirely.
    ("Credit Card", re.compile(r'(?<![0-9A-Fa-f]-)\b(?:3[47]\d{2}[- ]?\d{6}[- ]?\d{5}|(?:4\d{3}|5[1-5]\d{2}|3[47]\d{2}|6(?:011|5\d{2}))[- ]?\d{4}[- ]?\d{4}[- ]?\d{4})\b(?!-[0-9A-Fa-f])'), "Possible credit card number"),
    ("EIN/Tax ID", re.compile(r'\b\d{2}-\d{7}\b'), "Possible EIN/Tax ID number"),
    ("Phone Number", re.compile(r'\b(?:\+1[- ]?)?\(\d{3}\)[- ]?\d{3}[- ]?\d{4}\b'), "Possible phone number (with area code parens)"),
    ("Date of Birth", re.compile(r'(?i)\b(date[_\s\-]?of[_\s\-]?birth|dob)\s*[=:]\s*'), "Date of birth field"),
    ("Bank Account", re.compile(r'(?i)(account[_\s\-]?number|routing[_\s\-]?number|iban|swift|bic)\s*[=:]\s*["\']?\d'), "Bank account/routing number"),
    ("Salary/Compensation", re.compile(r'(?i)(salary|annual[_\s]?pay|hourly[_\s]?rate|compensation)\s*[=:]\s*[\$]?\d'), "Salary or compensation value"),
    ("MAC Address", re.compile(r'\b([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b'), "MAC address"),
    ("UUID/GUID", re.compile(r'(?i)(user[_\-]?id|account[_\-]?id|customer[_\-]?id)\s*[=:]\s*["\']?[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'), "Hardcoded user/account UUID"),
]

# ── Allowlists: suppress known false positives ──

# Matched text that is safe (exact or contained).  If the matched string
# contains any of these, the finding is dropped.
_MATCH_ALLOWLIST = {
    "127.0.0.1", "0.0.0.0",                     # standard bind/localhost
    "localhost:5050", "localhost:5051",           # VibeNode's own ports
    "http://localhost:", "https://localhost:",    # localhost URLs generally OK in source
}


def _luhn_ok(matched: str) -> bool:
    """True if the digits in ``matched`` satisfy the Luhn checksum.

    Every issued payment card number carries a Luhn check digit. A digit run
    that merely looks card-shaped — a UUID fragment, a timestamp, a test
    fixture id — passes only by ~1-in-10 chance. Requiring Luhn therefore
    removes the bulk of this pattern's false positives without ever
    suppressing a real card.
    """
    digits = [int(c) for c in matched if c.isdigit()]
    if not 13 <= len(digits) <= 19:
        return False
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


# Per-finding-type validators. A finding is reported only if its validator
# returns True. Types absent from this map are reported on regex match alone.
# Use this for patterns that have a real checksum or structural rule — it is
# far more precise than widening the regex or blanket-skipping a file.
_MATCH_VALIDATORS: Dict[str, Callable[[str], bool]] = {
    "Credit Card": _luhn_ok,
}

# Files (normalized forward-slash paths) to skip content scanning entirely.
# These are known to contain patterns that look like secrets but aren't.
_FILE_SKIP_PATTERNS = [
    re.compile(r'src/web/git_scanner\.py$'),          # scanner's own regex patterns
    re.compile(r'\.md$'),                          # markdown docs — prompts/templates, not secrets
    re.compile(r'src/features/workforce/'),                     # agent template instructions
    re.compile(r'docs/'),                          # documentation
    re.compile(r'CLAUDE\.md$'),                    # project instructions
]

# Specific file+type combos to suppress (file glob, finding type)
_FINDING_SUPPRESSIONS = [
    # SQL migrations are code, not private documents
    (re.compile(r'src/web/db/migrations/.*\.sql$'), None),
    # Test fixtures with fake paths like C:\Users\test\proj
    (re.compile(r'tests/'), "Hardcoded User Path"),
    # Workforce agent docs use classification terms as templates
    (re.compile(r'src/features/workforce/'), "Confidential Marker"),
    (re.compile(r'docs/'), "Confidential Marker"),
    # NOTE: session_store.py, src/features/pipeline/plan.py, src/engine/handoff_rules.py and
    # src/engine/smart_termination.py used to suppress "Env Variable Secret"
    # because of constants named *_KEY(S). The pattern now ignores values
    # that are code (see its comment), so those files are fully scanned again.
    # NOTE: prelogin_unlock.py previously needed a "Password Assignment"
    # suppression here. It no longer does — the root cause was the pattern's
    # value class matching newlines (see _SECRET_PATTERNS above). The
    # suppression was removed deliberately: that file serves the pre-login
    # unlock page, so it is exactly the file where a real hardcoded password
    # would be most dangerous, and it must stay in scope for that pattern.
]

# ── Who the owner is (names, user id, machine name) ──
# Never in any published file, notes and agent guides included: these rules run
# even on the files the rest of the scan skips (markdown, src/features/workforce/, docs/,
# CLAUDE.md), all but the scanner itself and binary files. Words written out in
# this file's own patterns would be published by the scanner itself, so they are
# kept only as fingerprints (``_fingerprint``):
# every word of a file is fingerprinted and compared, so this file never holds
# a name. Do NOT put a name back in plain text here, put identity rules behind
# the skip list, or exempt a folder from them. To add a word, compute
# ``_fingerprint("<word>")`` in a terminal and add the result, never the word.
_TOKEN = re.compile(r'[A-Za-z0-9]+')
_WORDY_NEXT = set("'\u2019&\\;")
_WORDY_PREV = set("'\u2019&")


@functools.lru_cache(maxsize=262144)
def _fingerprint(word: str) -> str:
    return hashlib.sha256(b'vibenode-identity\0' + word.encode('utf-8')).hexdigest()[:20]


class _IdentityRule:
    """One identity rule. ``case``: compare the word as written (else lowercased).
    ``initial``: one leading letter may come first (usernames, emails: d+surname).
    ``as_name``: not part of a contraction or an entity ("Don't", "Don&#x27;t")."""

    def __init__(self, label, desc, fingerprints, lengths, case=False, initial=False, as_name=False):
        self.label, self.desc = label, desc
        self.fingerprints, self.lengths = frozenset(fingerprints), frozenset(lengths)
        self.case, self.initial, self.as_name = case, initial, as_name

    @classmethod
    def of(cls, label, desc, words, **kw):
        """A rule from plain words: for tests and for computing fingerprints, never for this file."""
        return cls(label, desc, [_fingerprint(w if kw.get("case") else w.lower()) for w in words],
                   [len(w) for w in words], **kw)

    def word_hit(self, word: str) -> bool:
        tok = word if self.case else word.lower()
        cands = [tok]
        if self.initial and len(tok) > 1 and tok[0].isalpha():
            cands.append(tok[1:])
        return any(len(c) in self.lengths and _fingerprint(c) in self.fingerprints for c in cands)

    def hit(self, content: str, m) -> bool:
        if not self.word_hit(m.group(0)):
            return False
        if self.as_name:
            s, e = m.start(), m.end()
            if s > 0 and content[s - 1] in _WORDY_PREV:
                return False
            if e < len(content) and content[e] in _WORDY_NEXT:
                # A possessive ("<name>'s") is the name; any other contraction
                # ("Don't" style) or an entity is not.
                possessive = (content[e] in "'\u2019" and content[e + 1:e + 2].lower() == "s"
                              and not content[e + 2:e + 3].isalpha())
                if not possessive:
                    return False
        return True


_IDENTITY_RULES = [
    _IdentityRule("Owner ID", "Owner user ID found in file", ["c33db43f62b91d593675"], [5]),
    _IdentityRule("Owner Name", "Owner name found in file", ["b6dc452db520231f9aad"], [5]),
    _IdentityRule("Owner Family Name", "Owner family name found in file",
                  ["9317b6de8b4d245388a3", "11466ac13cedcf53c4e0", "15b56ed4782100111678"], [5, 6, 13],
                  initial=True),
    _IdentityRule("Owner First Name", "Owner first name found in file", ["d72540b6dc5a229f810a"], [3],
                  case=True, as_name=True),
    _IdentityRule("Collaborator Name", "Collaborator first name found in file", ["ba02680dcc2a17ceb4fb"], [7],
                  case=True, as_name=True),
    _IdentityRule("Owner Machine Name", "Owner machine name found in file", ["04a8272a6298084a87db"], [5]),
    # Swearing, usually a quote copied into a comment or a note.
    # Quote the point, not the words. Fingerprints only, like the rest.
    _IdentityRule("Swearing", "Swearing found in file (reword the quote)",
                  ['c9d484b35522f84380d9', '2f447b8a8c4c4cc4d7fa', '36634e75a8d810af9bbf', '9a834f04101ffd2f86bf',
                   '47146c5c592e0b63d5e4', 'ce1726d13472bd3e83b4', '7c9bb4aa93804d38e09f', '4eb5fa253ec2c90bc525',
                   'fc73a8c40bab836f8d43', '4cbe803afde3d9f761ee', '579dc178411587c78c1a', '69e74722a7a9670bdf8e',
                   'eacf83005bb9103b14a1', '5b3e366f97eaf4b02613', '916484ff9bc537ffb936', 'beadbe93d8d3227a57e0',
                   'd757c0c24b58d00d4b30', 'f263bca89c5ae9c7492a'], [4, 5, 6, 7, 8, 9, 12, 13]),
]

# ── The public layout ──
# .gitignore's top block is the one definition of what may be published. A
# tracked file those rules cover got in before them or by a force-add; the
# public project file (CLAUDE.md) holds only short public rules, and project
# notes live in the private docs/plans/PROJECT-NOTES.md. Both are refused here.
CLAUDE_MD_MAX_LINES = 60


def layout_problems(proj: Path = None) -> list:
    """``blocked_files`` entries for files outside the public layout."""
    proj = proj or _VIBENODE_DIR
    out = []
    try:
        r = subprocess.run(["git", "-C", str(proj), "ls-files", "-ci", "--exclude-standard"],
                           capture_output=True, text=True, timeout=10, creationflags=_NO_WINDOW)
        for f in (r.stdout.splitlines() if r.returncode == 0 else []):
            if f.strip() and (proj / f.strip()).exists():
                out.append({"file": f.strip(), "reason": "Not part of the public project (.gitignore): "
                            "remove it from git with `git rm --cached`; it stays on disk"})
    except Exception:
        pass
    md = proj / "CLAUDE.md"
    try:
        n = md.read_text(encoding="utf-8", errors="ignore").count("\n") if md.is_file() else 0
    except Exception:
        n = 0
    if n > CLAUDE_MD_MAX_LINES:
        out.append({"file": "CLAUDE.md", "reason": f"The public project file has {n} lines (limit "
                    f"{CLAUDE_MD_MAX_LINES}): project notes belong in docs/plans/PROJECT-NOTES.md, which is private"})
    return out


# ── Notes about people and build history ──
# Comments, docs and tests once quoted the owner, recorded what he picked from
# which mockup, and dated every change. None of that is for a public repository:
# state the rule and its technical reason instead. Like the identity rules these
# run on every published file, notes and guides included.
_NOTE_RULES = [
    ("Owner Quote", re.compile(r"(?i)\(\s*(?:the\s+)?owner\s*,\s*20\d\d-\d\d-\d\d"
                               r"|\bthe owner\s*(?:,\s*20\d\d-\d\d-\d\d)?\s*:\s*[\"\u201c']"),
     "A quote of the owner: state the rule and its reason instead"),
    ("Owner Decision Note", re.compile(r"(?i)\bowner(?:'s|\u2019s)\s+(?:pick|picks|choice|decision|words|request)\b"
                                       r"|\bthe owner\s+(?:asked|said|wrote|wants|wanted|reported|rejected|approved"
                                       r"|picked|chose|hated|complained)\b"),
     "A note about what the owner said or decided: state the rule and its reason instead"),
    ("Personal Quote", re.compile(r"(?i)\b(?:he|she|the user)\s+(?:said|asked|wrote|complained|reported)"
                                  r"\s*(?:[:,]\s*|\s+)[\"\u201c]"),
     "A note quoting or describing a person: state the rule and its reason instead"),
    ("Dated Build Note", re.compile(r"(?i)\b(?:fixed|added|changed|hardened|reverted|removed|updated|split|picked)"
                                    r"\s+(?:on\s+)?20\d\d-\d\d-\d\d\b"),
     "A dated build-history note: git history records when; say what and why"),
]


def _note_findings(content: str) -> list:
    out = []
    for label, rx, desc in _NOTE_RULES:
        for m in rx.finditer(content):
            out.append({"line": content[:m.start()].count('\n') + 1, "type": label,
                        "desc": desc, "match": m.group(0)[:6] + '***'})
    return out


_ESCAPE = re.compile(r"\\[nrtbfv0]")


def _identity_findings(content: str) -> list:
    # In source text a line break is written "\\n": blank such escapes (same
    # length, so line numbers hold) or "\\n\\nName" reads as one word "nName".
    content = _ESCAPE.sub("  ", content)
    rules = _IDENTITY_RULES
    lengths = set().union(*(r.lengths | {n + 1 for n in r.lengths} for r in rules)) if rules else set()
    out = []
    # Fingerprint each distinct word once; walk positions only when one matches.
    words = {w for w in set(_TOKEN.findall(content)) if len(w) in lengths}
    if not any(r.word_hit(w) for w in words for r in rules):
        return out
    for m in _TOKEN.finditer(content):
        if len(m.group(0)) not in lengths:
            continue
        for r in rules:
            if r.hit(content, m):
                w = m.group(0)
                out.append({"line": content[:m.start()].count('\n') + 1, "type": r.label,
                            "desc": r.desc, "match": w[:4] + '***'})
    return out


_SCANNER_ITSELF = re.compile(r'src/web/git_scanner\.py$')


def skipped(norm: str) -> bool:
    """True when a file (forward-slash path from the repo root) is not content-scanned at all."""
    if any(p.search(norm) for p in _FILE_SKIP_PATTERNS):
        return True
    return any(stype is None and spat.search(norm) for spat, stype in _FINDING_SUPPRESSIONS)


def scan_text(norm: str, content: str) -> list:
    """Every finding the Publish scan reports for ``content`` saved at ``norm``.

    The one content check: both scan loops below use it, and so does the
    Session Engine's write guard (``src/engine/publish_guard.py``, which runs it in
    a separate interpreter because the engine never imports ``app``), so a
    session is refused a name or a key when it writes it, not at Publish.
    Each finding is ``{"line", "type", "desc", "match"}``, the match redacted.
    """
    if _SCANNER_ITSELF.search(norm):
        return []
    out = _identity_findings(content) + _note_findings(content)
    if skipped(norm):
        return out
    for label, pattern, desc in _SECRET_PATTERNS:
        if any(stype == label and spat.search(norm) for spat, stype in _FINDING_SUPPRESSIONS):
            continue
        for match in pattern.finditer(content):
            matched = match.group(0)
            # Check match allowlist: skip known-safe values
            if any(safe in matched for safe in _MATCH_ALLOWLIST):
                continue
            # Check type validator (e.g. Luhn for card numbers)
            validator = _MATCH_VALIDATORS.get(label)
            if validator is not None and not validator(matched):
                continue
            # Redact aggressively: show minimal context
            display = matched[:6] + '***REDACTED***' if len(matched) > 12 else matched[:4] + '***'
            out.append({"line": content[:match.start()].count('\n') + 1, "type": label,
                        "desc": desc, "match": display})
    return out


# Files that should NEVER be committed (beyond .gitignore)
_FORBIDDEN_FILES = [
    re.compile(r'\.env[._\-]?.*$', re.IGNORECASE),      # .env, .env.local, .env_backup, etc.
    re.compile(r'credentials\.json$', re.IGNORECASE),
    re.compile(r'service[_-]?account.*\.json$', re.IGNORECASE),
    re.compile(r'token\.json$', re.IGNORECASE),           # Google OAuth token cache
    re.compile(r'id_(rsa|ed25519|ecdsa|dsa)$'),           # SSH keys by algorithm
    re.compile(r'.*_rsa$'),                                # Legacy SSH key naming
    re.compile(r'.*\.pem$'),                               # Certificates
    re.compile(r'.*\.p12$'),                               # PKCS12
    re.compile(r'.*\.pfx$'),                               # PKCS12
    re.compile(r'.*\.p8$'),                                # Apple auth keys
    re.compile(r'.*\.keystore$', re.IGNORECASE),           # Java/Android keystores
    re.compile(r'.*\.jks$', re.IGNORECASE),                # Java keystores
    re.compile(r'\.htpasswd$', re.IGNORECASE),             # Apache password files
    re.compile(r'\.npmrc$'),                               # npm auth tokens
    re.compile(r'\.pypirc$'),                              # PyPI credentials
    re.compile(r'terraform\.tfstate(\.backup)?$'),         # Terraform state (contains secrets)
    re.compile(r'kanban_config\.json$'),                   # VibeNode-specific secrets file
]

# Private/business documents that should never be in a code repo.
# These get blocked by EXTENSION alone — no filename check needed.
_PRIVATE_DOC_EXTENSIONS = {
    # Office documents
    '.docx', '.doc', '.xlsx', '.xls', '.pptx', '.ppt', '.odt', '.ods', '.odp',
    # PDFs
    '.pdf',
    # Scanned documents
    '.tiff', '.tif', '.bmp',
    # Data exports
    '.csv', '.tsv',
    # Email exports
    '.eml', '.msg', '.mbox', '.pst',
    # Database exports
    '.dump',
    # Accounting / finance
    '.qbw', '.qbb', '.ofx', '.qfx', '.qif',
    # Rich text / ebooks
    '.rtf', '.epub',
}

# Filename patterns that indicate private/business documents.
# These match ANY extension — catches invoice.html, contract.md, nda.txt, etc.
_PRIVATE_DOC_PATTERNS = [
    # Short acronyms (nda, loi, mou, ssn) must stand alone between non-letters:
    # as bare substrings "nda" matched "standard" (playbook_standard.py) and
    # "mou" would match "mouse". "nda.pdf" and "client_nda.md" still block.
    re.compile(r'(?i)(invoice|receipt|contract|agreement|(?<![a-z])nda(?![a-z])|tax[-_]?return|w[_-]?2|w[_-]?9|1099|pay[_-]?stub|payroll)'),
    re.compile(r'(?i)(statement|balance[_-]?sheet|profit[_-]?loss|p&l|financ|budget|forecast|expense[_-]?report)'),
    re.compile(r'(?i)(medical|hipaa|diagnosis|prescription|insurance[_-]?claim|patient)'),
    re.compile(r'(?i)(passport|driver[_-]?licen|social[_-]?security|(?<![a-z])ssn(?![a-z])|birth[_-]?cert|visa[_-]?app)'),
    re.compile(r'(?i)(confidential|proprietary|trade[_-]?secret|internal[_-]?only|do[_-]?not[_-]?distribute)'),
    re.compile(r'(?i)(customer[_-]?list|client[_-]?list|employee[_-]?list|salary|compensation|roster)'),
    re.compile(r'(?i)(board[_-]?minutes|meeting[_-]?notes|legal[_-]?memo|attorney|litigation|settlement)'),
    re.compile(r'(?i)(proposal|quote|estimate|purchase[_-]?order|po[_-]?\d|work[_-]?order|scope[_-]?of[_-]?work)'),
    re.compile(r'(?i)(letter[_-]?of[_-]?intent|(?<![a-z])loi(?![a-z])|term[_-]?sheet|(?<![a-z])mou(?![a-z])|memorandum)'),
    re.compile(r'(?i)(resume|curriculum[_-]?vitae|cv[_-]|cover[_-]?letter|job[_-]?offer|offer[_-]?letter)'),
]

# Max file size to scan for secrets. Larger files are reported as unscannable
# so a human reviews them. The limit is a scan-budget cap, not a security
# judgement — regex scanning stays well under a second at this size, and the
# identity rules and "Hardcoded User Path" pattern still need to run against
# large legitimate source files (CSS, bundled JS). Raise before adding
# per-extension skips: skipping by type silently exempts real patterns from
# the scan, size does not.
_MAX_SCAN_SIZE = 2_000_000

# Binary/large files that shouldn't be in a repo
_BINARY_EXTENSIONS = {
    '.exe', '.dll', '.so', '.dylib', '.bin', '.dat', '.db', '.sqlite', '.sqlite3',
    '.zip', '.tar', '.gz', '.rar', '.7z', '.mp4', '.avi', '.mov', '.mp3', '.wav',
    '.jar', '.war', '.class', '.wasm', '.img', '.iso', '.dmg', '.msi', '.deb', '.rpm',
}


def scan_outgoing_commits(proj: Path = None) -> list:
    """Identity findings in the versions a push would publish (``@{u}..HEAD``).

    The working-tree scan cannot see history. After a history rewrite removes
    identity material from every published version, a copy that still holds
    the old versions would publish it again by merging instead of replaying. Each outgoing version's added lines and message go through the
    identity rules, so that push is refused. ``[]`` when there is no upstream.
    """
    proj = proj or _VIBENODE_DIR
    try:
        r = subprocess.run(["git", "-C", str(proj), "log", "-p", "--no-color", "--format=%x00%H%n%B", "@{u}..HEAD"],
                           capture_output=True, text=True, timeout=60, creationflags=_NO_WINDOW,
                           encoding="utf-8", errors="ignore")
    except Exception:
        return []
    if r.returncode != 0:
        return []
    out = []
    for chunk in r.stdout.split("\x00")[1:]:
        sha, _, body = chunk.partition("\n")
        added = "\n".join(l[1:] for l in body.splitlines()
                          if not (l.startswith("+++") or l.startswith("---") or l.startswith("-")))
        for f in _identity_findings(added) + _note_findings(added):
            out.append({"commit": sha[:10], **f})
    return out


def count_scannable_files(proj: Path = None) -> Dict:
    """Quick file count for progress UI — no scanning, just lists files."""
    proj = proj or _VIBENODE_DIR
    try:
        r1 = subprocess.run(
            ["git", "-C", str(proj), "ls-files"],
            capture_output=True, text=True, timeout=10, creationflags=_NO_WINDOW)
        r2 = subprocess.run(
            ["git", "-C", str(proj), "ls-files", "--others", "--exclude-standard"],
            capture_output=True, text=True, timeout=10, creationflags=_NO_WINDOW)
        files = set()
        for r in (r1, r2):
            if r.returncode == 0:
                files.update(f.strip() for f in r.stdout.splitlines() if f.strip())
        return {"count": len(files)}
    except Exception:
        return {"count": 0}


def scan_staged_files(proj: Path = None) -> Dict:
    """
    Scan ALL tracked files + untracked files for secrets before push.
    Returns: {"ok": bool, "findings": [...], "blocked_files": [...], "summary": str}
    """
    proj = proj or _VIBENODE_DIR
    findings = []
    blocked_files = []

    # Get ALL files that would exist in the repo after a push:
    # 1. All currently tracked files (the full repo contents)
    # 2. Untracked files that would be added by `git add -A`
    try:
        r1 = subprocess.run(
            ["git", "-C", str(proj), "ls-files"],
            capture_output=True, text=True, timeout=10, creationflags=_NO_WINDOW)
        r2 = subprocess.run(
            ["git", "-C", str(proj), "ls-files", "--others", "--exclude-standard"],
            capture_output=True, text=True, timeout=10, creationflags=_NO_WINDOW)
    except Exception as e:
        return {"ok": False, "findings": [], "blocked_files": [], "summary": f"Scan error: {e}"}

    all_files = set()
    for r in (r1, r2):
        if r.returncode == 0:
            all_files.update(f.strip() for f in r.stdout.splitlines() if f.strip())

    if not all_files:
        return {"ok": True, "findings": [], "blocked_files": [], "summary": "No files to scan."}

    for fpath in sorted(all_files):
        full = proj / fpath
        norm = fpath.replace('\\', '/')

        if _SCANNER_ITSELF.search(norm):
            continue
        if skipped(norm):
            # Only the identity rules (scan_text); no file-type blocking
            if full.is_file() and not full.is_symlink() and full.stat().st_size <= _MAX_SCAN_SIZE:
                raw = full.read_bytes()
                if b'\x00' in raw[:8192]:
                    continue  # a picture or other binary: its bytes are not words
                text = raw.decode('utf-8', errors='ignore')
                findings.extend({"file": fpath, **f} for f in scan_text(norm, text))
            continue

        # Check forbidden file patterns
        is_forbidden = False
        for pat in _FORBIDDEN_FILES:
            if pat.search(fpath):
                blocked_files.append({"file": fpath, "reason": f"Forbidden file pattern: {pat.pattern}"})
                is_forbidden = True
                break
        if is_forbidden:
            continue

        # Check private/business document extensions
        ext = Path(fpath).suffix.lower()
        if ext in _PRIVATE_DOC_EXTENSIONS:
            blocked_files.append({"file": fpath, "reason": f"Private document type: {ext} — not safe for a public repo"})
            continue

        # Check private document filename patterns (any extension)
        fname_only = Path(fpath).name
        for pat in _PRIVATE_DOC_PATTERNS:
            if pat.search(fname_only):
                blocked_files.append({"file": fpath, "reason": f"Filename suggests private/business document: {fname_only}"})
                break

        # Check binary extensions
        if ext in _BINARY_EXTENSIONS:
            blocked_files.append({"file": fpath, "reason": f"Binary/large file type: {ext}"})
            continue

        # Scan file contents for secrets
        if not full.is_file() or full.is_symlink():
            continue
        try:
            fsize = full.stat().st_size
            if fsize > _MAX_SCAN_SIZE:
                blocked_files.append({"file": fpath, "reason": f"File too large to scan ({fsize:,} bytes) — review manually"})
                continue
            content = full.read_text(encoding='utf-8', errors='ignore')
        except Exception as e:
            blocked_files.append({"file": fpath, "reason": f"Could not read file for scanning: {e}"})
            continue

        findings.extend({"file": fpath, **f} for f in scan_text(norm, content))

    blocked_files.extend(layout_problems(proj))

    has_issues = bool(findings) or bool(blocked_files)
    summary_parts = []
    if findings:
        summary_parts.append(f"{len(findings)} potential secret(s) found")
    if blocked_files:
        summary_parts.append(f"{len(blocked_files)} blocked file(s)")
    summary = ". ".join(summary_parts) + "." if summary_parts else "All clear — no secrets or sensitive files detected."

    return {
        "ok": not has_issues,
        "findings": findings[:50],
        "blocked_files": blocked_files[:20],
        "summary": summary,
        "files_scanned": len(all_files),
    }


def scan_staged_files_stream(proj: Path = None):
    """Generator version that yields progress events as SSE lines.

    Yields JSON strings: {"type":"progress","current":n,"total":m,"file":"..."}
    Final yield:         {"type":"done", ...full scan result...}
    """
    import json as _json

    proj = proj or _VIBENODE_DIR
    findings = []
    blocked_files = []

    try:
        r1 = subprocess.run(
            ["git", "-C", str(proj), "ls-files"],
            capture_output=True, text=True, timeout=10, creationflags=_NO_WINDOW)
        r2 = subprocess.run(
            ["git", "-C", str(proj), "ls-files", "--others", "--exclude-standard"],
            capture_output=True, text=True, timeout=10, creationflags=_NO_WINDOW)
    except Exception as e:
        yield _json.dumps({"type": "done", "ok": False, "findings": [],
                           "blocked_files": [], "summary": f"Scan error: {e}",
                           "files_scanned": 0})
        return

    all_files = set()
    for r in (r1, r2):
        if r.returncode == 0:
            all_files.update(f.strip() for f in r.stdout.splitlines() if f.strip())

    total = len(all_files)
    sorted_files = sorted(all_files)

    for idx, fpath in enumerate(sorted_files):
        # Emit progress every file
        yield _json.dumps({"type": "progress", "current": idx + 1,
                           "total": total, "file": fpath})

        full = proj / fpath
        norm = fpath.replace('\\', '/')

        if _SCANNER_ITSELF.search(norm):
            continue
        if skipped(norm):
            # Only the identity rules (scan_text); no file-type blocking
            if full.is_file() and not full.is_symlink() and full.stat().st_size <= _MAX_SCAN_SIZE:
                raw = full.read_bytes()
                if b'\x00' in raw[:8192]:
                    continue  # a picture or other binary: its bytes are not words
                text = raw.decode('utf-8', errors='ignore')
                findings.extend({"file": fpath, **f} for f in scan_text(norm, text))
            continue

        is_forbidden = False
        for pat in _FORBIDDEN_FILES:
            if pat.search(fpath):
                blocked_files.append({"file": fpath, "reason": f"Forbidden file pattern: {pat.pattern}"})
                is_forbidden = True
                break
        if is_forbidden:
            continue

        ext = Path(fpath).suffix.lower()
        if ext in _PRIVATE_DOC_EXTENSIONS:
            blocked_files.append({"file": fpath, "reason": f"Private document type: {ext} — not safe for a public repo"})
            continue

        fname_only = Path(fpath).name
        for pat in _PRIVATE_DOC_PATTERNS:
            if pat.search(fname_only):
                blocked_files.append({"file": fpath, "reason": f"Filename suggests private/business document: {fname_only}"})
                break

        if ext in _BINARY_EXTENSIONS:
            blocked_files.append({"file": fpath, "reason": f"Binary/large file type: {ext}"})
            continue

        if not full.is_file() or full.is_symlink():
            continue
        try:
            fsize = full.stat().st_size
            if fsize > _MAX_SCAN_SIZE:
                blocked_files.append({"file": fpath, "reason": f"File too large to scan ({fsize:,} bytes) — review manually"})
                continue
            content = full.read_text(encoding='utf-8', errors='ignore')
        except Exception as e:
            blocked_files.append({"file": fpath, "reason": f"Could not read file for scanning: {e}"})
            continue

        findings.extend({"file": fpath, **f} for f in scan_text(norm, content))

    blocked_files.extend(layout_problems(proj))

    has_issues = bool(findings) or bool(blocked_files)
    summary_parts = []
    if findings:
        summary_parts.append(f"{len(findings)} potential secret(s) found")
    if blocked_files:
        summary_parts.append(f"{len(blocked_files)} blocked file(s)")
    summary = ". ".join(summary_parts) + "." if summary_parts else "All clear — no secrets or sensitive files detected."

    yield _json.dumps({
        "type": "done",
        "ok": not has_issues,
        "findings": findings[:50],
        "blocked_files": blocked_files[:20],
        "summary": summary,
        "files_scanned": total,
    })
