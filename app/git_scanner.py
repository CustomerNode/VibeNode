"""
Deterministic secret & sensitive-data scanner for pre-push validation.

Scans ALL tracked + untracked files for secrets, API keys, tokens, passwords,
credentials, and PII before allowing a git push. Fully deterministic —
no AI involved, just regex pattern matching.
"""

import re
import subprocess
import sys
from pathlib import Path
from typing import Dict

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
    ("Password Assignment", re.compile(r'(?i)(password|passwd|pwd)\s*[=:]\s*["\'][^"\']{8,}["\']'), "Hardcoded password"),

    # ── Cryptographic material ──
    ("Private Key", re.compile(r'-----BEGIN (RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----'), "Private key file"),
    ("JWT Token", re.compile(r'eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}'), "Hardcoded JWT"),

    # ── Encoded secrets ──
    ("Base64 Secret", re.compile(r'(?i)(secret|key|token|password|credential|auth)\s*[=:]\s*["\']?[A-Za-z0-9+/]{40,}={0,2}["\']?'), "Possible base64-encoded secret"),

    # ── Environment variable patterns ──
    ("Env Variable Secret", re.compile(r'(?i)^[A-Z_]*(SECRET|TOKEN|KEY|PASSWORD|CREDENTIAL|AUTH)[A-Z_]*\s*=\s*[^\s]{8,}', re.MULTILINE), "Environment variable with secret value"),

    # ── PII / personal data ──
    ("Hardcoded User Path", re.compile(r'(?i)["\'][A-Z]:\\\\Users\\\\[a-zA-Z][a-zA-Z0-9._\-]{2,}\\\\|["\']/(?:Users|home)/[a-zA-Z][a-zA-Z0-9._\-]{2,}/'), "Hardcoded personal filesystem path"),

    # ── Owner-specific personal identifiers ──
    # These protect the repo owner's PII from accidentally being committed.
    # The patterns use negative lookahead/behind to avoid matching encoded project
    # paths like "C--Users-15512-Documents-..." which are system-generated.
    ("Owner ID (15512)", re.compile(r'(?<!-)(?<!\w)15512(?!-\d)(?!\w)'), "Owner user ID found in file"),
    ("Owner Name (donca)", re.compile(r'(?i)\bdonca\b'), "Owner name found in file"),
    ("Owner Name (canto)", re.compile(r'(?i)\bcanto(?:w)?\b'), "Owner family name found in file"),

    # ── Internal network ──
    ("Internal IP URL", re.compile(r'https?://(?:192\.168\.|10\.|172\.(?:1[6-9]|2[0-9]|3[01])\.)\d+'), "Internal/private network URL"),
    ("Internal Hostname", re.compile(r'https?://[a-z][a-z0-9\-]+\.(internal|local|corp|lan|home|intranet)\b'), "Internal/corporate hostname"),

    # ── Sensitive document content ──
    ("Confidential Marker", re.compile(r'(?i)\b(CONFIDENTIAL|DO NOT DISTRIBUTE|INTERNAL USE ONLY|TRADE SECRET|ATTORNEY[\s\-]CLIENT PRIVILEGED|NOT FOR PUBLIC RELEASE|STRICTLY PRIVATE)\b'), "Document contains confidentiality marker"),
    ("SSN Pattern", re.compile(r'\b\d{3}-\d{2}-\d{4}\b'), "Possible Social Security Number"),
    ("Credit Card", re.compile(r'\b(?:4\d{3}|5[1-5]\d{2}|3[47]\d{2}|6(?:011|5\d{2}))[- ]?\d{4}[- ]?\d{4}[- ]?\d{4}\b'), "Possible credit card number"),
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

# Files (normalized forward-slash paths) to skip content scanning entirely.
# These are known to contain patterns that look like secrets but aren't.
_FILE_SKIP_PATTERNS = [
    re.compile(r'app/git_scanner\.py$'),          # scanner's own regex patterns
    re.compile(r'\.md$'),                          # markdown docs — prompts/templates, not secrets
    re.compile(r'workforce/'),                     # agent template instructions
    re.compile(r'docs/'),                          # documentation
    re.compile(r'CLAUDE\.md$'),                    # project instructions
]

# Specific file+type combos to suppress (file glob, finding type)
_FINDING_SUPPRESSIONS = [
    # SQL migrations are code, not private documents
    (re.compile(r'app/db/migrations/.*\.sql$'), None),
    # Test fixtures with fake paths like C:\Users\test\proj
    (re.compile(r'tests/'), "Hardcoded User Path"),
    (re.compile(r'tests/'), "Owner ID (15512)"),
    # Workforce agent docs use classification terms as templates
    (re.compile(r'workforce/'), "Confidential Marker"),
    (re.compile(r'docs/'), "Confidential Marker"),
]

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
    re.compile(r'(?i)(invoice|receipt|contract|agreement|nda|tax[-_]?return|w[_-]?2|w[_-]?9|1099|pay[_-]?stub|payroll)'),
    re.compile(r'(?i)(statement|balance[_-]?sheet|profit[_-]?loss|p&l|financ|budget|forecast|expense[_-]?report)'),
    re.compile(r'(?i)(medical|hipaa|diagnosis|prescription|insurance[_-]?claim|patient)'),
    re.compile(r'(?i)(passport|driver[_-]?licen|social[_-]?security|ssn|birth[_-]?cert|visa[_-]?app)'),
    re.compile(r'(?i)(confidential|proprietary|trade[_-]?secret|internal[_-]?only|do[_-]?not[_-]?distribute)'),
    re.compile(r'(?i)(customer[_-]?list|client[_-]?list|employee[_-]?list|salary|compensation|roster)'),
    re.compile(r'(?i)(board[_-]?minutes|meeting[_-]?notes|legal[_-]?memo|attorney|litigation|settlement)'),
    re.compile(r'(?i)(proposal|quote|estimate|purchase[_-]?order|po[_-]?\d|work[_-]?order|scope[_-]?of[_-]?work)'),
    re.compile(r'(?i)(letter[_-]?of[_-]?intent|loi|term[_-]?sheet|mou|memorandum)'),
    re.compile(r'(?i)(resume|curriculum[_-]?vitae|cv[_-]|cover[_-]?letter|job[_-]?offer|offer[_-]?letter)'),
]

# Binary/large files that shouldn't be in a repo
_BINARY_EXTENSIONS = {
    '.exe', '.dll', '.so', '.dylib', '.bin', '.dat', '.db', '.sqlite', '.sqlite3',
    '.zip', '.tar', '.gz', '.rar', '.7z', '.mp4', '.avi', '.mov', '.mp3', '.wav',
    '.jar', '.war', '.class', '.wasm', '.img', '.iso', '.dmg', '.msi', '.deb', '.rpm',
}


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

        # Skip files in the file-level skip list (scanner itself, docs, templates)
        if any(p.search(norm) for p in _FILE_SKIP_PATTERNS):
            continue

        # Check file+type suppressions (e.g. SQL migrations are code)
        _suppressed_file = False
        for spat, stype in _FINDING_SUPPRESSIONS:
            if spat.search(norm) and stype is None:
                _suppressed_file = True
                break
        if _suppressed_file:
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
            if fsize > 500_000:
                blocked_files.append({"file": fpath, "reason": f"File too large to scan ({fsize:,} bytes) — review manually"})
                continue
            content = full.read_text(encoding='utf-8', errors='ignore')
        except Exception as e:
            blocked_files.append({"file": fpath, "reason": f"Could not read file for scanning: {e}"})
            continue

        for label, pattern, desc in _SECRET_PATTERNS:
            # Check per-file type suppressions
            _type_suppressed = False
            for spat, stype in _FINDING_SUPPRESSIONS:
                if stype == label and spat.search(norm):
                    _type_suppressed = True
                    break
            if _type_suppressed:
                continue

            for match in pattern.finditer(content):
                matched = match.group(0)
                # Check match allowlist — skip known-safe values
                if any(safe in matched for safe in _MATCH_ALLOWLIST):
                    continue
                line_start = content[:match.start()].count('\n') + 1
                # Redact aggressively — show minimal context
                if len(matched) > 12:
                    display = matched[:6] + '***REDACTED***'
                else:
                    display = matched[:4] + '***'
                findings.append({
                    "file": fpath,
                    "line": line_start,
                    "type": label,
                    "desc": desc,
                    "match": display,
                })

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

        # Skip files in the file-level skip list
        if any(p.search(norm) for p in _FILE_SKIP_PATTERNS):
            continue

        # Check file+type suppressions (e.g. SQL migrations)
        _suppressed_file = False
        for spat, stype in _FINDING_SUPPRESSIONS:
            if spat.search(norm) and stype is None:
                _suppressed_file = True
                break
        if _suppressed_file:
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
            if fsize > 500_000:
                blocked_files.append({"file": fpath, "reason": f"File too large to scan ({fsize:,} bytes) — review manually"})
                continue
            content = full.read_text(encoding='utf-8', errors='ignore')
        except Exception as e:
            blocked_files.append({"file": fpath, "reason": f"Could not read file for scanning: {e}"})
            continue

        for label, pattern, desc in _SECRET_PATTERNS:
            # Check per-file type suppressions
            _type_suppressed = False
            for spat, stype in _FINDING_SUPPRESSIONS:
                if stype == label and spat.search(norm):
                    _type_suppressed = True
                    break
            if _type_suppressed:
                continue

            for match in pattern.finditer(content):
                matched = match.group(0)
                # Check match allowlist — skip known-safe values
                if any(safe in matched for safe in _MATCH_ALLOWLIST):
                    continue
                line_start = content[:match.start()].count('\n') + 1
                if len(matched) > 12:
                    display = matched[:6] + '***REDACTED***'
                else:
                    display = matched[:4] + '***'
                findings.append({
                    "file": fpath, "line": line_start,
                    "type": label, "desc": desc, "match": display,
                })

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
