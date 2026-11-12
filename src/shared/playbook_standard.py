"""The Playbook Standard: what every VibeNode playbook must contain.

A playbook is a ``src/features/workforce/*.md`` entry whose frontmatter says
``playbook: true``. This module is the ONE definition of what such an entry
must hold. VibeNode enforces it itself, so a playbook written by hand, by a
session, or by the Playbook Builder is held to the same rules:

* ``POST /api/workforce/write-asset`` refuses to save a playbook that fails;
* ``GET /api/workforce/assets`` does not list a failing playbook in
  Settings → Playbooks (it stays a plain workforce entry) and reports why;
* ``tests/test_playbook_standard.py`` fails the suite (and so the publish
  gate) when any playbook in ``src/features/workforce/`` fails.

The rules cover seven parts: identity, purpose, inputs, execution, outputs,
verification and safety. They are proportional: ``playbook_risk`` says what a
playbook can touch, and the higher the risk, the more it must spell out.

    low     talks and thinks only (brainstorming, drafting in the chat)
    medium  creates things: files, sessions, documents
    high    changes code, runs commands or experiments, or sends anything out

``playbook_capabilities`` lists what it does (``read``, ``write``,
``run_commands``, ``external_read``, ``delete``, ``publish``, ``send_out``).
Each capability has a minimum risk (``CAPABILITIES``). A playbook must declare
every capability its own instructions show (``detect_capabilities``), and its
risk may not be lower than its strongest capability. Detection is deliberately
cautious and is NOT a security boundary: it keeps a playbook honest about what
it says it does. What a session may actually run is still decided by VibeNode's
own permission policy, for every session, whatever playbook it follows.

Across the library, ids, names, invocation words and example phrases must be
unique (``library_problems``); a clash blocks every playbook in it until one is
changed.

Command line (no server needed):

    python -m shared.playbook_standard                 check every playbook in src/features/workforce/
    python -m shared.playbook_standard <file.md> ...   check these files
    python -m shared.playbook_standard --explain       print the standard

This module imports nothing from ``app`` or ``daemon`` (tests/test_layering.py).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

RISKS = ("low", "medium", "high")
_RANK = {r: i for i, r in enumerate(RISKS)}

# A glance, not a page: what Settings → Playbooks shows must stay short.
LINE_MAX_WORDS = 8
WHAT_MAX_WORDS = 22
STEP_MAX_WORDS = 14
STEPS_MIN, STEPS_MAX = 3, 6
SAY_MIN = 2
CONTRACT_MIN, CONTRACT_MAX = 3, 6  # the authoring standard's Rule 2

# Each part: (name, what it is, the lowest risk at which it is required).
PARTS = (
    ("identity", "id, name, version, playbook_line, playbook_say, playbook_use and playbook_risk in the frontmatter, and an `Invoke by typing:` line", "low"),
    ("purpose", "a `**Unique value**:` line saying what no other playbook does, and playbook_what (one sentence, at most 22 words)", "low"),
    ("inputs", "`## Invocation Contract`: 3 to 6 bullets naming what the caller must pass in", "low"),
    ("execution", "`## The Prompt` with the instructions, and playbook_how: 3 to 6 short steps", "low"),
    ("outputs", "`## Output Format`: a numbered list; from medium risk it ends on what was not validated, then obstacles encountered", "low"),
    ("verification", "`## Standing Criteria` (or `## Verification`): the checks it always makes, at least 2 bullets", "medium"),
    ("safety", "`## Escalate when`, `## Fix Policy` or `## Safety`: when it stops and asks; at high risk it also says in its prompt what it must not touch", "medium"),
)

_VERIFY_HEADS = ("standing", "verification")
_SAFETY_HEADS = ("escalate when", "fix policy", "safety")

# What a playbook can do, and the lowest risk that may declare it.
CAPABILITIES = {
    "read": "low",              # reads files, the chat, or this machine's own pages
    "write": "medium",          # creates local artifacts: a report, notes, a draft, a temporary file
    "run_commands": "medium",   # runs a shell command or a script
    "read_only_sessions": "medium",  # starts new sessions that can only read and answer (VibeNode's
                                     # read-only plan mode): they cannot edit files or run commands
    "external_read": "medium",  # fetches from the internet or a third-party service
    "delete": "high",           # deletes or overwrites data
    "publish": "high",          # pushes, merges, deploys or publishes anything
    "send_out": "high",         # sends data off this machine: email, upload, post
    "modify_code": "high",      # changes application source, configuration or a repository
    "run_experiments": "high",  # runs experiments, A/B tests or new sessions that can change code or app state
}

# Cautious signals for each capability in a playbook's own text. A match in a
# sentence that forbids it before the action ("Do not delete …") does not count.
_CAP_SIGNALS = {
    "run_commands": r"`(?:python3?|py|pytest|git|npm|npx|node|pip|curl|wget|bash|sh|powershell|pwsh|cmd)\b"
                    r"|\b(?:run|execute)s?\s+(?:the\s+|a\s+|this\s+)?(?:command|script|shell)"
                    r"|\bbash tool\b|\bpowershell\b|\bsubprocess\b",
    "write": r"\b(?:writes?|creates?|saves?|edits?|modif(?:y|ies)|renames?)\b",
    "modify_code": r"\b(?:edit\w*|modif\w*|chang\w*|patch\w*|refactor\w*|rewrit\w*|implement\w*|fix(?:es|ed|ing)?|updat\w*)\b"
                   r"[^.\n]{0,60}?\b(?:source(?: code)?|code ?base|application code|app code|the code|production code"
                   r"|repo(?:sitory)?|modules?|functions?|app/|src/|daemon/|static/js|\w+\.(?:py|js|ts|tsx|jsx|css|go|rs|java|cs))\b"
                   r"|\bgit\s+(?:commit|apply|am|cherry-pick|rebase)\b"
                   r"|\bcommit(?:s|ted|ting)?\s+(?:the\s+|it\s+|your\s+|a\s+)?(?:change|fix|code|work)"
                   r"|\bapply\s+(?:the\s+|a\s+)?(?:patch|diff)\b",
    "run_experiments": r"\b(?:run|runs|running|start|starts|starting|launch\w*|spawn\w*|conduct\w*)\b[^.\n]{0,40}?"
                       r"\b(?:experiments?|a/b tests?|ab tests?|benchmarks?|variants?|trials?)\b"
                       r"|\b(?:start|starts|launch\w*|spawn\w*)\b[^.\n]{0,40}?\b(?:new |fresh |real )?sessions?\b",
    "external_read": r"https?://(?!(?:127\.0\.0\.1|localhost)\b)|\bweb ?search\b|\bwebfetch\b|\bcurl\b|\bwget\b"
                     r"|\brequests\.get\b|\bthird[- ]party\b|\bexternal (?:api|service)",
    "delete": r"\b(?:delet(?:e|es|ed|ing)|eras(?:e|es|ing)|wip(?:e|es|ing)|purg(?:e|es|ing)|destroy(?:s|ing)?"
              r"|truncat(?:e|es|ing)|drop\s+table|overwrit(?:e|es|ing))\b"
              r"|\brm\s+-|\brmdir\b|\bdel\s+/|remove-item|shutil\.rmtree|os\.(?:remove|unlink)|\.unlink\("
              r"|git\s+(?:reset\s+--hard|clean\b|push\s+(?:-f|--force))",
    "publish": r"\bgit\s+push\b|\bpush(?:es|ed|ing)?\s+(?:it\s+|the\s+\w+\s+)?to\s+(?:origin|github|the remote|remote|main|production)"
               r"|\bpublish(?:es|ed)?\b|\bdeploy(?:s|ed)?\b|\bnpm\s+publish\b|\bgh\s+(?:pr|release)\s+create\b"
               r"|\bmerg(?:e|es|ing)\s+(?:it\s+)?into\s+(?:main|master|production)",
    "send_out": r"\b(?:e-?mail(?:s|ed|ing)?|upload(?:s|ed|ing)?|transmit(?:s|ted|ting)?|exfiltrat\w*|transfer(?:s|red|ring)?)\b"
                r"|\b(?:send|post|forward|share)(?:s|ed|ing)?\b[^.\n]{0,60}\b(?:outside|external|third[- ]party|address"
                r"|server|endpoint|webhook|slack|customer|recipient|anyone)\b"
                r"|requests\.post|curl\s+-X\s*POST|curl\s+(?:-d|--data)\b",
}
_CAP_RE = {k: re.compile(v, re.I) for k, v in _CAP_SIGNALS.items()}
# A prohibition governs an action only when it names it directly ("Do not
# delete", "never push the branch"). "Do not ask permission before deleting"
# and "never wait to delete" are instructions TO delete: the words between the
# prohibition and the action turn it around.
_FORBIDS = re.compile(r"\b(?:do not|don't|never|must not|mustn't|no|not|nothing|refuses?|forbid\w*|avoid\w*)\b", re.I)
_TURNS_AROUND = re.compile(r"\b(?:ask\w*|permission|confirm\w*|wait\w*|check\w*|hesitat\w*|before|prompt\w*|need|needs"
                           r"|bother|worry|stop\w*|approv\w*|tell|warn\w*|mention\w*|until|delay\w*)\b", re.I)
_FORBID_REACH = 8  # at most this many words between a prohibition and the action it forbids


def _forbidden(before: str) -> bool:
    """Does the text before an action, in the same sentence, forbid that action?"""
    last = None
    for last in _FORBIDS.finditer(before):
        pass
    if last is None:
        return False
    gap = before[last.end():]
    if len(gap.split()) > _FORBID_REACH:
        return False
    return not _TURNS_AROUND.search(gap)

# A high-risk playbook must bound itself in its own words.
_BOUNDS = re.compile(
    r"\b(?:do not|don't|never|must not|only (?:touch|edit|change|write|create|run)|read-only)\b", re.I)


def parse_frontmatter(content: str):
    """Simple ``key: value`` frontmatter, the same reading as the workforce loader.

    Returns ``(frontmatter, body)``. Lists ``[a, b]`` split on commas, ``true``
    / ``false`` become booleans, ``null`` or empty become None.
    """
    frontmatter: dict = {}
    body = content
    if content.startswith('---'):
        parts = content.split('---', 2)
        if len(parts) >= 3:
            for line in parts[1].strip().splitlines():
                line = line.strip()
                if not line or line.startswith('#') or ':' not in line:
                    continue
                k, v = line.split(':', 1)
                k, v = k.strip(), v.strip()
                if v.startswith('[') and v.endswith(']'):
                    v = [x.strip().strip("'\"") for x in v[1:-1].split(',') if x.strip()]
                elif v.lower() == 'true':
                    v = True
                elif v.lower() == 'false':
                    v = False
                elif v.lower() == 'null' or v == '':
                    v = None
                else:
                    v = v.strip("'\"")
                frontmatter[k] = v
            body = parts[2].strip()
    return frontmatter, body


def is_playbook(frontmatter: dict) -> bool:
    """``playbook: true``, or any ``playbook_*`` field: dropping the flag from a
    file that still carries a playbook's fields does not take it out of reach."""
    if frontmatter.get('playbook') is True:
        return True
    return any(str(k).startswith('playbook_') for k in frontmatter)


def _sentences(text: str):
    return [x for x in re.split(r"(?<=[.!?;])\s+|\n+", text or "") if x.strip()]


# A sentence that launches sessions or variants AND says they are read-only
# shows `read_only_sessions`, not `run_experiments`:
# a session that can only read cannot change code or state. Like every signal
# this is an honesty check, not a boundary; the runner must enforce the mode.
_READ_ONLY = re.compile(r"\bread-only\b", re.I)


def detect_capabilities(body: str) -> set:
    """Capabilities a playbook's own words show it uses (cautious, not a boundary)."""
    found = set()
    for sent in _sentences(body):
        for cap, rx in _CAP_RE.items():
            if cap == "run_experiments" and _READ_ONLY.search(sent):
                if any(not _forbidden(sent[:m.start()]) for m in rx.finditer(sent)):
                    found.add("read_only_sessions")
                continue
            for m in rx.finditer(sent):
                # `delete` in backticks names the capability; it is not an action.
                if (m.group(0).lower() in CAPABILITIES and sent[m.start() - 1:m.start()] == "`"
                        and sent[m.end():m.end() + 1] == "`"):
                    continue
                if not _forbidden(sent[:m.start()]):
                    found.add(cap)
                    break
    return found


def declared_capabilities(frontmatter: dict):
    caps = frontmatter.get("playbook_capabilities")
    if caps is None:
        return None
    if isinstance(caps, str):
        caps = [c for c in re.split(r"[,|\s]+", caps) if c]
    return [str(c).strip().lower() for c in caps]


def required_risk(caps) -> str:
    """The lowest risk these capabilities allow; an unknown one counts as high."""
    return max((CAPABILITIES.get(c, "high") for c in caps), key=lambda r: _RANK[r], default="low")


def _norm(s) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", str(s or "").lower())).strip()


def invoke_words(body: str) -> list:
    m = re.search(r"Invoke by typing:\s*\*\*([^*]+)\*\*", body or "")
    return [m.group(1).strip()] if m else []


def library_problems(entries) -> dict:
    """Clashes across playbooks: ``{filename: [problem, ...]}``.

    ``entries`` is an iterable of ``(filename, content)``; only playbooks are
    compared. Ids, names and the Try-it phrase must be unique, and no words
    that start one playbook (its invocation words or an example phrase) may
    start another. Both sides of a clash get the problem, so neither is offered
    until one is changed.
    """
    seen: dict = {}
    out: dict = {}
    for fname, content in entries:
        fm, body = parse_frontmatter(content or "")
        if not is_playbook(fm):
            continue
        keys = [("id", "id", fm.get("id") or Path(fname).stem), ("name", "name", fm.get("name")),
                ("Try-it phrase", "use", fm.get("playbook_use"))]
        keys += [("invocation words", "phrase", w) for w in invoke_words(body)]
        keys += [("example phrase", "phrase", p) for p in say_list(fm)]
        for kind, space, val in keys:
            n = _norm(val)
            if not n:
                continue
            other = seen.get((space, n))
            if other and other != fname:
                out.setdefault(fname, []).append(f"identity: {kind} \"{val}\" is also used by {Path(other).name}")
                out.setdefault(other, []).append(f"identity: {kind} \"{val}\" is also used by {Path(fname).name}")
            else:
                seen.setdefault((space, n), fname)
    return out


def _sections(body: str) -> dict:
    """``## Heading`` (lower-case) -> its text, up to the next ``## ``."""
    out: dict = {}
    cur = None
    for line in body.splitlines():
        m = re.match(r"^##\s+(.+?)\s*$", line)
        if m and not line.startswith('###'):
            cur = m.group(1).strip().lower()
            out[cur] = []
        elif cur is not None:
            out[cur].append(line)
    return {k: "\n".join(v) for k, v in out.items()}


def _find(sections: dict, starts) -> str | None:
    for k, v in sections.items():
        if any(k.startswith(s) for s in starts):
            return v
    return None


def _bullets(text: str) -> list:
    return [l for l in text.splitlines() if re.match(r"^\s*[-*]\s+\S", l)]


def _numbered(text: str) -> list:
    return [l for l in text.splitlines() if re.match(r"^\s*\d+[.)]\s+\S", l)]


def _words(s) -> int:
    return len(str(s or "").split())


def say_list(frontmatter: dict) -> list:
    say = frontmatter.get('playbook_say') or ''
    if isinstance(say, list):
        say = ' | '.join(say)
    return [s.strip() for s in str(say).split('|') if s.strip()]


def steps(frontmatter: dict) -> list:
    return [s.strip() for s in str(frontmatter.get('playbook_how') or '').split('|') if s.strip()]


def floor_risk(body: str) -> str:
    """The lowest risk this playbook's own instructions allow it to declare."""
    return required_risk(detect_capabilities(body))


def check(content: str, filename: str | None = None) -> list:
    """Every way ``content`` falls short of the standard, as plain sentences.

    An empty list means it meets the standard. A file that is not a playbook
    (no ``playbook: true``) returns an empty list: the standard is for
    playbooks only.
    """
    fm, body = parse_frontmatter(content or "")
    if not is_playbook(fm):
        return []
    problems: list = []

    def need(part, msg):
        problems.append(f"{part}: {msg}")

    # -- identity -------------------------------------------------------
    for key in ("id", "name", "version"):
        if not fm.get(key):
            need("identity", f"`{key}:` is missing from the frontmatter")
    if fm.get("id") and not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", str(fm["id"])):
        need("identity", "`id:` must be kebab-case (lower-case words joined by hyphens)")
    if filename and fm.get("id") and Path(filename).stem != str(fm["id"]):
        need("identity", f"`id: {fm['id']}` does not match the file name {Path(filename).name}")
    if fm.get("version") and not re.fullmatch(r"\d+\.\d+\.\d+", str(fm["version"])):
        need("identity", "`version:` must be semver, like 1.0.0")
    line = fm.get("playbook_line")
    if not line:
        need("identity", "`playbook_line:` is missing (the one line Settings → Playbooks shows)")
    elif _words(line) > LINE_MAX_WORDS:
        need("identity", f"`playbook_line:` is {_words(line)} words; at most {LINE_MAX_WORDS}")
    say = say_list(fm)
    if len(say) < SAY_MIN:
        need("identity", f"`playbook_say:` needs at least {SAY_MIN} example phrases, separated by |")
    if not fm.get("playbook_use"):
        need("identity", "`playbook_use:` is missing (the phrase Try it puts in the box, like \"Use AI compare\")")
    risk = fm.get("playbook_risk")
    if risk not in RISKS:
        need("identity", "`playbook_risk:` must be low, medium or high (what the playbook can touch)")
        risk = "high"  # judge an undeclared playbook by the strictest rules
    if not re.search(r"Invoke by typing:\s*\*\*[^*]+\*\*", body):
        need("identity", "the body needs an `Invoke by typing: **<words>**` line")

    # -- purpose --------------------------------------------------------
    if not re.search(r"^\*\*Unique value\*\*:\s*\S", body, re.M):
        need("purpose", "the body needs a `**Unique value**:` line saying what no other playbook does")
    what = fm.get("playbook_what")
    if not what:
        need("purpose", "`playbook_what:` is missing (one sentence on what it does)")
    elif _words(what) > WHAT_MAX_WORDS:
        need("purpose", f"`playbook_what:` is {_words(what)} words; at most {WHAT_MAX_WORDS}")

    sec = _sections(body)

    # -- inputs ---------------------------------------------------------
    contract = _find(sec, ("invocation contract",))
    if contract is None:
        need("inputs", "`## Invocation Contract` is missing")
    else:
        n = len(_bullets(contract))
        if not CONTRACT_MIN <= n <= CONTRACT_MAX:
            need("inputs", f"`## Invocation Contract` has {n} bullets; it needs {CONTRACT_MIN} to {CONTRACT_MAX}")

    # -- execution ------------------------------------------------------
    prompt = _find(sec, ("the prompt",))
    if prompt is None or not prompt.strip():
        need("execution", "`## The Prompt` is missing or empty")
    st = steps(fm)
    if not STEPS_MIN <= len(st) <= STEPS_MAX:
        need("execution", f"`playbook_how:` has {len(st)} steps; it needs {STEPS_MIN} to {STEPS_MAX}, separated by |")
    for s in st:
        if _words(s) > STEP_MAX_WORDS:
            need("execution", f"step \"{s[:40]}…\" is {_words(s)} words; at most {STEP_MAX_WORDS}")

    # -- capabilities and the risk floor --------------------------------
    if fm.get("playbook") is not True:
        need("identity", "`playbook: true` is missing, but the file carries playbook fields; it is held to the standard either way")
    declared = declared_capabilities(fm)
    detected = detect_capabilities(body)
    if declared is None:
        need("identity", "`playbook_capabilities:` is missing: list what it does, from " + ", ".join(CAPABILITIES))
        declared = []
    unknown = [c for c in declared if c not in CAPABILITIES]
    if unknown:
        need("identity", f"`playbook_capabilities:` has unknown {', '.join(unknown)}; use only " + ", ".join(CAPABILITIES))
    missing = [c for c in CAPABILITIES if c in detected and c not in declared]
    if missing:
        need("identity", "its instructions show " + ", ".join(missing) + " but `playbook_capabilities:` does not declare it")
    caps = set(declared) | detected
    floor = required_risk(caps)
    if _RANK[risk] < _RANK[floor]:
        strongest = sorted(c for c in caps if CAPABILITIES.get(c, "high") == floor)
        need("identity", f"`playbook_risk: {risk}` is too low: {', '.join(strongest)} needs at least {floor}")
        risk = floor

    # -- outputs --------------------------------------------------------
    out = _find(sec, ("output format",))
    if out is None:
        need("outputs", "`## Output Format` is missing")
    else:
        items = _numbered(out)
        if not items:
            need("outputs", "`## Output Format` must be a numbered list")
        elif _RANK[risk] >= _RANK["medium"]:
            tail = " ".join(items[-2:]).lower()
            if len(items) < 2 or "not validated" not in tail or "obstacle" not in items[-1].lower():
                need("outputs", "`## Output Format` must end on \"What was not validated\" and then \"Obstacles encountered\"")

    # -- verification ---------------------------------------------------
    if _RANK[risk] >= _RANK["medium"]:
        ver = _find(sec, _VERIFY_HEADS)
        if ver is None:
            need("verification", f"a {risk}-risk playbook needs `## Standing Criteria` (or `## Verification`)")
        elif len(_bullets(ver)) < 2:
            need("verification", "the verification section needs at least 2 bullets")

    # -- safety ---------------------------------------------------------
    if _RANK[risk] >= _RANK["medium"]:
        saf = _find(sec, _SAFETY_HEADS)
        if saf is None:
            need("safety", f"a {risk}-risk playbook needs `## Escalate when`, `## Fix Policy` or `## Safety`")
        elif not _bullets(saf):
            need("safety", "the safety section needs at least one bullet saying when to stop and ask")
    if risk == "high" and not _BOUNDS.search(prompt or ""):
        need("safety", "a high-risk playbook must say in `## The Prompt` what it must not touch (\"Do not …\", \"Never …\")")

    return problems


def explain() -> str:
    lines = [
        "VibeNode Playbook Standard",
        "",
        "A playbook is a workforce/*.md entry with `playbook: true` in its frontmatter.",
        "`playbook_risk` sets how much it must spell out:",
        "  low     reads and talks only",
        "  medium  creates local artifacts, runs commands, fetches from the web, or starts read-only sessions",
        "  high    deletes, publishes, sends data out, changes code, or runs experiments",
        "The risk may never be lower than the highest capability declared.",
        "",
        "Parts (required from the risk shown):",
    ]
    for name, what, frm in PARTS:
        lines.append(f"  {name:<13}[{frm}+] {what}")
    lines += ["", "Capabilities (playbook_capabilities) and the lowest risk each allows:"]
    for c, r in CAPABILITIES.items():
        lines.append(f"  {c:<17}{r}")
    lines += [
        "",
        "Ids, names, invocation words and example phrases must be unique across the library",
        "(exact matches, ignoring case and punctuation). Replacing a playbook must raise its version.",
        "",
        "Enforced by VibeNode: registration (--register, /api/workforce/write-asset) refuses a",
        "failing playbook; loading it to run (--show, /api/playbooks/<id>) checks it again and",
        "refuses; Settings > Playbooks does not list it; the test suite fails on it.",
        "A declared risk grants no permission: every action still goes through VibeNode's checks.",
    ]
    return "\n".join(lines)


def _default_files() -> list:
    wf = Path(__file__).resolve().parents[1] / "features" / "workforce"
    return sorted(wf.glob("*.md"))


def library_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "features" / "workforce"


def read_library(folder=None) -> dict:
    """``{filename: content}`` for every ``*.md`` in the library folder."""
    out = {}
    folder = Path(folder) if folder else library_dir()
    for f in sorted(folder.glob("*.md")) if folder.is_dir() else []:
        try:
            out[f.name] = f.read_text(encoding="utf-8")
        except OSError:
            pass
    return out


def problems_in_library(content: str, filename: str, library: dict) -> list:
    """Everything wrong with ``content`` saved as ``filename`` in ``library``:
    its own rules plus clashes with the other playbooks. ``library`` is not
    changed. The one check every path uses: registration, loading, listing."""
    probs = list(check(content, filename))
    if is_playbook(parse_frontmatter(content)[0]):
        lib = dict(library)
        lib[filename] = content
        probs += library_problems(lib.items()).get(filename, [])
    return probs


def find_playbook(pid: str, library: dict):
    """``(filename, content)`` of the library playbook with this id, or None."""
    for fname, content in library.items():
        fm, _ = parse_frontmatter(content)
        if is_playbook(fm) and str(fm.get("id") or Path(fname).stem) == pid:
            return fname, content
    return None


def load_playbook(pid: str, library=None) -> dict:
    """The one way to run a playbook: its definition, checked NOW.

    Returns ``{ok, id, name, risk, capabilities, body}`` when the playbook in
    the library passes the standard at this moment, else ``{ok: False,
    problems}``. Nothing is cached, so a playbook changed on disk is checked
    again on its next load.
    """
    library = read_library() if library is None else library
    hit = find_playbook(pid, library)
    if not hit:
        return {"ok": False, "id": pid, "problems": [f"no playbook with id {pid} in the library"]}
    fname, content = hit
    probs = problems_in_library(content, fname, library)
    if probs:
        return {"ok": False, "id": pid, "problems": probs}
    fm, body = parse_frontmatter(content)
    return {"ok": True, "id": pid, "name": fm.get("name"), "risk": fm.get("playbook_risk"),
            "capabilities": declared_capabilities(fm), "body": body}


def register(draft: Path, folder=None, replaces: str | None = None) -> tuple:
    """Check a draft and copy it into the library only when it passes.

    ``replaces`` renames a playbook: the draft takes over from the playbook
    registered under that id, which is removed in the same step, only when the
    draft passes. The draft is checked against the library WITHOUT the old one
    (so it may keep the old one's words), and its version must be higher (a
    rename is a change). Without it, a rename meant registering a duplicate
    and deleting the old file by hand, outside the standard.

    Returns ``(ok, problems, target)``. On a failure the library is untouched.
    """
    folder = Path(folder) if folder else library_dir()
    content = Path(draft).read_text(encoding="utf-8")
    fm, _ = parse_frontmatter(content)
    pid = str(fm.get("id") or "")
    if not is_playbook(fm):
        return False, ["identity: the draft is not a playbook (no `playbook: true`)"], None
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", pid):
        return False, ["identity: `id:` must be kebab-case"], None
    target = folder / f"{pid}.md"
    library = read_library(folder)
    old_file = None
    if replaces:
        old_file = folder / f"{replaces}.md"
        old = library.get(old_file.name)
        if replaces == pid:
            return False, ["identity: --replaces names the draft's own id; a change to a playbook needs no rename"], target
        if old is None or not is_playbook(parse_frontmatter(old)[0]):
            return False, [f"identity: there is no playbook {replaces!r} in the library to replace"], target
        if target.name in library:
            return False, [f"identity: {pid!r} is already in the library; a rename needs a new id"], target
        library = {k: v for k, v in library.items() if k != old_file.name}
        probs = replacement_problems(old, content)
        if probs:
            return False, [f"identity: a rename must raise `version:` above {replaces}'s "
                           f"({parse_frontmatter(old)[0].get('version')})"], target
    # The draft is judged under its own file name first, so a draft whose id
    # names another playbook fails here instead of quietly taking its slot.
    probs = list(check(content, Path(draft).name))
    probs += [p for p in problems_in_library(content, target.name, library) if p not in probs]
    probs += replacement_problems(library.get(target.name), content)
    if probs:
        return False, probs, target
    folder.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".md.tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(content)
    tmp.replace(target)
    if old_file is not None:
        old_file.unlink()
    return True, [], target


def _version(v):
    try:
        return tuple(int(x) for x in str(v).split("."))
    except ValueError:
        return None


def replacement_problems(old, new) -> list:
    """Replacing a library playbook is a change: it must raise ``version:``.

    Without this a new playbook that reused an existing id replaced it outright."""
    if old is None:
        return []
    old_fm, _ = parse_frontmatter(old)
    if not is_playbook(old_fm):
        return []
    new_fm, _ = parse_frontmatter(new)
    ov, nv = _version(old_fm.get("version")), _version(new_fm.get("version"))
    if ov and nv and nv > ov:
        return []
    return [f"identity: this replaces the playbook already registered as {old_fm.get('id')} "
            f"(version {old_fm.get('version')}); a change must raise `version:`, and a new playbook needs a new id"]


USAGE = """python -m shared.playbook_standard [files...] [--into <library folder>]
    check playbooks (no files: every playbook in the library)
python -m shared.playbook_standard --register <draft.md> [--replaces <old id>] [--into <library folder>]
    check a draft and copy it into the library only when it passes
    (--replaces: a rename; the old playbook is removed in the same step)
python -m shared.playbook_standard --show <id> [--into <library folder>]
    print a playbook's instructions, only if it passes the standard now
python -m shared.playbook_standard --explain
    print the standard"""


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    if "--help" in argv or "-h" in argv:
        print(USAGE)
        return 0
    if "--explain" in argv:
        print(explain())
        return 0
    into = None
    if "--into" in argv:
        i = argv.index("--into")
        into = argv[i + 1]
        del argv[i:i + 2]
    if "--register" in argv:
        argv.remove("--register")
        replaces = None
        if "--replaces" in argv:
            i = argv.index("--replaces")
            replaces = argv[i + 1]
            del argv[i:i + 2]
            if len(argv) != 1:
                print("--replaces takes exactly one draft")
                return 1
        bad = 0
        for a in argv:
            ok, probs, target = register(Path(a), into, replaces=replaces)
            if ok and replaces:
                print(f"replaced {replaces}: removed {replaces}.md from the library")
            if ok:
                print(f"registered {Path(a).name} as {target}")
            else:
                bad += 1
                print(f"REFUSED {Path(a).name}: nothing in the library changed")
                for p in probs:
                    print(f"  - {p}")
        return 1 if bad else 0
    if "--show" in argv:
        argv.remove("--show")
        res = load_playbook(argv[0], read_library(into))
        if res["ok"]:
            print(res["body"])
            return 0
        if not find_playbook(argv[0], read_library(into)):
            print(f"REFUSED {argv[0]}: there is no playbook with that id in the library")
            return 1
        print(f"REFUSED {argv[0]}: it does not meet the Playbook Standard now")
        for p in res["problems"]:
            print(f"  - {p}")
        return 1
    files = [Path(a) for a in argv] or (sorted(Path(into).glob("*.md")) if into else _default_files())
    bad = 0
    checked = 0
    library = read_library(into)
    clashes = library_problems(library.items())
    for f in files:
        try:
            content = f.read_text(encoding="utf-8")
        except OSError as e:
            print(f"FAIL {f}: cannot read ({e})")
            bad += 1
            continue
        fm, _ = parse_frontmatter(content)
        if not is_playbook(fm):
            if argv:
                print(f"skip {f.name}: not a playbook (no `playbook: true`)")
            continue
        checked += 1
        in_library = f.resolve().parent == (Path(into) if into else library_dir()).resolve()
        probs = (check(content, f.name) + clashes.get(f.name, [])) if in_library \
            else problems_in_library(content, f.name, library) + replacement_problems(library.get(f.name), content)
        if probs:
            bad += 1
            print(f"FAIL {f.name}")
            for p in probs:
                print(f"  - {p}")
        else:
            print(f"ok   {f.name} ({fm.get('playbook_risk')} risk)")
    print(f"{checked} playbook(s) checked, {bad} failing")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
