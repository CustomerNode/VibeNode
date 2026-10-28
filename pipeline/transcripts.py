"""Read what the Session Engine recorded: tool calls, results and subagents.

The CLI writes one JSONL transcript per session under
``~/.claude/projects/<encoded cwd>/<session id>.jsonl`` and one per subagent
under ``<session id>/subagents/agent-<id>.jsonl`` with a sibling
``agent-<id>.meta.json`` whose ``toolUseId`` is the ``Agent`` tool call in the
parent that launched it.  Shapes verified against real transcripts on
2026-10-27:

* an ``assistant`` entry's ``message.content`` holds ``tool_use`` blocks
  ``{id, name, input}``;
* a ``user`` entry's ``message.content`` holds ``tool_result`` blocks
  ``{tool_use_id, content, is_error}`` and, for Bash, the entry's top-level
  ``toolUseResult`` is ``{stdout, stderr, interrupted, ...}``; a failing
  command has ``is_error: true`` and content starting ``Error: Exit code N``;
* a subagent transcript's first ``user`` entry is the kickoff prompt, as a
  string or as ``text`` blocks.

These records are the harness's only source of truth about what ran.  A stage
file an agent wrote is never evidence; the transcript that shows it being
written is.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator

from common.paths import claude_projects_dir, encode_cwd


@dataclass
class ToolCall:
    """One tool invocation and, when the transcript has it, its result."""

    transcript: Path
    tool_use_id: str
    name: str
    input: dict
    result_text: str = ""
    is_error: bool | None = None
    stdout: str = ""
    stderr: str = ""
    timestamp: str = ""

    @property
    def command(self) -> str:
        return str(self.input.get("command") or "")

    @property
    def file_path(self) -> str:
        """The file a Write/Edit/NotebookEdit call targeted, whatever the tool
        calls its argument."""
        return str(self.input.get("file_path") or self.input.get("notebook_path") or self.input.get("path") or "")

    @property
    def output(self) -> str:
        """Everything the command produced that the engine kept."""
        return "\n".join(s for s in (self.stdout, self.stderr, self.result_text) if s)


@dataclass
class Transcript:
    path: Path
    is_subagent: bool
    agent_id: str = ""
    meta: dict = field(default_factory=dict)
    kickoff_prompt: str = ""
    calls: list[ToolCall] = field(default_factory=list)

    def calls_named(self, *names: str) -> list[ToolCall]:
        return [c for c in self.calls if c.name in names]


def _iter_entries(path: Path) -> Iterator[dict]:
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if isinstance(d, dict):
                yield d


def _content_blocks(entry: dict) -> list[dict]:
    msg = entry.get("message")
    if not isinstance(msg, dict):
        return []
    content = msg.get("content")
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
    return []


def _content_text(entry: dict) -> str:
    msg = entry.get("message")
    if not isinstance(msg, dict):
        return ""
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(b.get("text") or "") for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def _result_text(block: dict) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(b.get("text") or "") for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def read_transcript(path: Path, *, is_subagent: bool) -> Transcript:
    """Parse one JSONL transcript into tool calls with their results."""
    t = Transcript(path=path, is_subagent=is_subagent)
    by_id: dict[str, ToolCall] = {}
    kickoff_seen = False
    for entry in _iter_entries(path):
        etype = entry.get("type")
        if etype == "assistant":
            for b in _content_blocks(entry):
                if b.get("type") != "tool_use":
                    continue
                call = ToolCall(
                    transcript=path,
                    tool_use_id=str(b.get("id") or ""),
                    name=str(b.get("name") or ""),
                    input=b.get("input") if isinstance(b.get("input"), dict) else {},
                    timestamp=str(entry.get("timestamp") or ""),
                )
                t.calls.append(call)
                if call.tool_use_id:
                    by_id[call.tool_use_id] = call
        elif etype == "user":
            if is_subagent and not kickoff_seen:
                text = _content_text(entry)
                if text:
                    t.kickoff_prompt = text
                    kickoff_seen = True
                    if not t.agent_id:
                        t.agent_id = str(entry.get("agentId") or "")
            tur = entry.get("toolUseResult")
            for b in _content_blocks(entry):
                if b.get("type") != "tool_result":
                    continue
                call = by_id.get(str(b.get("tool_use_id") or ""))
                if call is None:
                    continue
                call.result_text = _result_text(b)
                call.is_error = bool(b.get("is_error"))
                if isinstance(tur, dict):
                    call.stdout = str(tur.get("stdout") or "")
                    call.stderr = str(tur.get("stderr") or "")
                elif isinstance(tur, str) and not call.result_text:
                    call.result_text = tur
    return t


def project_transcripts_dir(project_cwd: str) -> Path:
    """Where the CLI keeps the root transcripts of sessions started in *project_cwd*."""
    return claude_projects_dir() / encode_cwd(project_cwd)


def iter_root_transcripts(project_cwd: str) -> Iterable[Path]:
    """Root transcripts of the project, newest first."""
    d = project_transcripts_dir(project_cwd)
    if not d.is_dir():
        return []
    return sorted(d.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)


def find_root_transcript(project_cwd: str, marker: str) -> Path | None:
    """The root transcript whose recorded tool output contains *marker*.

    ``pipeline.start`` prints a nonce the run's ``run.json`` also holds; the
    session that ran it is the run's root session.  A byte-level search comes
    first so a project with hundreds of transcripts is not parsed in full.
    """
    needle = marker.encode("utf-8")
    for path in iter_root_transcripts(project_cwd):
        try:
            with open(path, "rb") as fh:
                if needle not in fh.read():
                    continue
        except OSError:
            continue
        # The bytes are there; now insist the line was PRINTED by a
        # ``pipeline.start`` call in this transcript.  A later session that
        # quotes the line (``cat 00-request.md``, a grep) is not the root.
        t = read_transcript(path, is_subagent=False)
        for c in t.calls_named("Bash", "PowerShell"):
            if marker in c.output and _is_start_call(c.command):
                return path
    return None


def _is_start_call(command: str) -> bool:
    cmd = command.replace("\\", "/")
    return "pipeline.start" in cmd or "pipeline/start" in cmd


def subagents_of(root: Path) -> list[Transcript]:
    """Every subagent transcript the CLI kept for *root*, with its meta."""
    sdir = root.with_suffix("") / "subagents"
    out: list[Transcript] = []
    if not sdir.is_dir():
        return out
    for jsonl in sorted(sdir.glob("agent-*.jsonl")):
        t = read_transcript(jsonl, is_subagent=True)
        meta_path = jsonl.with_suffix(".meta.json")
        if meta_path.is_file():
            try:
                t.meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except ValueError:
                t.meta = {}
        if not t.agent_id:
            t.agent_id = jsonl.stem.removeprefix("agent-")
        out.append(t)
    return out


def same_file(a: str, b: str | Path) -> bool:
    """Path equality that survives slashes, case and drive letters on Windows."""
    try:
        pa = os.path.normcase(os.path.normpath(os.path.abspath(str(a))))
        pb = os.path.normcase(os.path.normpath(os.path.abspath(str(b))))
    except (TypeError, ValueError):
        return False
    return pa == pb


def under_dir(path: str, directory: str | Path) -> bool:
    """True when *path* is *directory* or anything inside it (same normalisation as ``same_file``)."""
    try:
        p = os.path.normcase(os.path.normpath(os.path.abspath(str(path))))
        d = os.path.normcase(os.path.normpath(os.path.abspath(str(directory))))
    except (TypeError, ValueError):
        return False
    return p == d or p.startswith(d.rstrip("\\/") + os.sep)


WRITE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")


def writers_of(transcripts: Iterable[Transcript], target: str | Path) -> list[tuple[Transcript, ToolCall]]:
    """Every (transcript, call) that wrote or edited *target*."""
    hits = []
    for t in transcripts:
        for c in t.calls_named(*WRITE_TOOLS):
            if c.file_path and same_file(c.file_path, target):
                hits.append((t, c))
    return hits


def writes_under(transcripts: Iterable[Transcript], directory: str | Path) -> list[tuple[Transcript, ToolCall]]:
    """Every (transcript, call) that wrote or edited a file inside *directory* with a file tool."""
    hits = []
    for t in transcripts:
        for c in t.calls_named(*WRITE_TOOLS):
            if c.file_path and under_dir(c.file_path, directory):
                hits.append((t, c))
    return hits


# A shell command that can create or change a file: redirects, copies, moves,
# the PowerShell writers, and an inline interpreter.  Reading the evidence
# (cat, type, Get-Content, jq) is what an audit does and is not matched.
_SHELL_WRITE_RE = re.compile(
    r"(?<![<\d])>|\btee\b|\bcp\b|\bmv\b|\bcopy\b|\bmove\b|\bSet-Content\b|\bOut-File\b|"
    r"\bAdd-Content\b|\bCopy-Item\b|\bMove-Item\b|\bNew-Item\b|\bpython3?\s+-c\b|\bnode\s+-e\b|"
    r"\bsed\s+-i\b|\btouch\b|\brm\b|\bdel\b|\bRemove-Item\b",
    re.IGNORECASE,
)


# The delimiter must be a whole word (optionally quoted as a whole) ending at
# a shell boundary: ``<<E"O"F`` has delimiter ``EOF`` in bash, and reading it
# as ``E`` let a body run on past bash's real terminator (Review, loop 6).
_HEREDOC_RE = re.compile(r"<<-?\s*(['\"]?)(\w+)\1(?=$|[\s;&|<>)])")
# A heredoc fed to one of these is text being written somewhere, not code.
_DATA_SINKS = re.compile(r"^\s*(?:cat|tee)\b")
# Where one command of a header line ends and the next begins.
_SEGMENT_SPLIT_RE = re.compile(r"\|\|?|;|&&?|\$\(|\(|`")


def strip_data_heredocs(command: str) -> str:
    """The command with the bodies of heredocs that only carry text removed.

    ``cat >> 03-review.md <<'EOF' … EOF`` appends a report; the report may
    NAME an evidence or output file, and the command also has a ``>``, so a
    check that saw both read a forgery where there was none (found on the
    evidence-gaps run).  Only heredocs fed to ``cat`` or ``tee`` are stripped:
    one fed to ``python``, ``node`` or a shell is code and stays.

    Fail-safe rules (Review, loop 6): the command that owns the heredoc (the
    segment the ``<<`` is in) must be ``cat`` or ``tee``, and every later
    segment of the header line too, so ``python - <<EOF | cat`` and
    ``cat <<EOF | sh`` keep their bodies; a body is stripped only when its
    terminator line is found, so an unterminated or misread heredoc hides
    nothing."""
    lines = command.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        m = _HEREDOC_RE.search(line)
        data = False
        if m:
            owner = _SEGMENT_SPLIT_RE.split(line[:m.start()])[-1]
            later = [s for s in _SEGMENT_SPLIT_RE.split(line[m.end():])[1:] if s.strip()]
            data = bool(_DATA_SINKS.match(owner)) and all(_DATA_SINKS.match(s) for s in later)
        if data:
            delim = m.group(2)
            end = next((j for j in range(i + 1, len(lines)) if lines[j].strip() == delim), None)
            if end is not None:
                out.append(lines[end])
                i = end
        i += 1
    return "\n".join(out)


def shell_writes_under(transcripts: Iterable[Transcript], directory: str | Path) -> list[tuple[Transcript, ToolCall]]:
    """Bash/PowerShell calls whose command text names *directory*.

    The evidence folder must only ever be written by ``pipeline.run_check``;
    a shell redirect, ``cp`` or ``Set-Content`` into it is a forgery attempt.
    """
    d = str(directory).replace("\\", "/").rstrip("/")
    tail = d.rsplit("/", 1)[-1]
    hits = []
    for t in transcripts:
        for c in t.calls_named("Bash", "PowerShell"):
            cmd = strip_data_heredocs(c.command).replace("\\", "/")
            if not (f"/{tail}/" in cmd or cmd.rstrip().endswith(f"/{tail}")):
                continue
            if is_evidence_writer_call(cmd):
                continue
            if _SHELL_WRITE_RE.search(cmd):
                hits.append((t, c))
    return hits


# The programs that are allowed to write into the evidence folder, and whose
# printed lines the harness accepts as produced (not quoted).
EVIDENCE_WRITERS = ("run_check", "attest", "escalate")


def is_pipeline_call(command: str, module: str) -> bool:
    """True when a shell command invokes ``pipeline.<module>`` and nothing
    runs after it.

    The harness accepts a program's printed line (``PIPELINE-EVIDENCE``,
    ``PIPELINE-ATTEST``, ``PIPELINE-ESCALATE``) only from such a call.  Any
    command can print such a line (``echo`` it after hashing a hand-written
    file); a line from a command that never ran the program is not evidence.
    And nothing may run AFTER the program in the same command: a compound
    ``run_check …; python forge.py`` invokes run_check and then rewrites the
    evidence and prints a matching line, all under one recorded command.  A
    prefix (``cd repo &&``) is allowed; a suffix operator is not.
    """
    cmd = command.replace("\\", "/")
    idx = max(cmd.find(f"pipeline.{module}"), cmd.find(f"pipeline/{module}"))
    if idx < 0:
        return False
    if _SHELL_CHAIN_RE.search(cmd[idx:]):
        return False
    # The prefix may only be a directory change and the interpreter.  An
    # environment assignment before the program (``PYTEST_ADDOPTS='-k one'
    # python -m pipeline.run_check …``, ``env X=1 …``, ``$env:X='…'; …``)
    # changes what the verified command does while the program records a
    # genuine passing run.  Found by the Test Team of the evidence-chain run.
    return _ALLOWED_PREFIX_RE.fullmatch(cmd[:idx]) is not None


def is_run_check_call(command: str) -> bool:
    """``is_pipeline_call(command, "run_check")``."""
    return is_pipeline_call(command, "run_check")


def is_evidence_writer_call(command: str) -> bool:
    """True when the command invokes one of the programs allowed to write
    under ``evidence/`` (chained or not; the chain rule is applied where the
    line is consumed)."""
    cmd = command.replace("\\", "/")
    return any(f"pipeline.{m}" in cmd or f"pipeline/{m}" in cmd for m in EVIDENCE_WRITERS)


# A bare ``&`` is a chain too: a sequential separator in cmd.exe, and in bash
# it backgrounds the program and runs the rest at once (``run_check … & perl
# -e 'sleep 30; forge'`` rewrites the evidence after run_check wrote it, under
# one recorded command).  ``2>&1`` and ``&>`` are redirects, not chains.
# Before ``pipeline.<module>``: optionally ``cd <dir> &&`` (or ``;``, or
# PowerShell's ``Set-Location``), then the interpreter (bare, quoted, or after
# PowerShell's ``&`` call operator), then ``-m``.  Nothing else.  Neither the
# directory nor the interpreter may contain a separator or an expansion:
# ``cd foo;$env:PYTEST_ADDOPTS='-k one';python -m …`` read as a directory
# named ``foo;$env:…`` followed by ``;``, and PowerShell evaluates
# ``"$($env:X='y';'.')"`` in the caller's scope, so a ``$(`` inside the quotes
# sets the variable too (found by the Review Team, loop 1).
_ALLOWED_PREFIX_RE = re.compile(
    r"""\s*(?:(?:cd|Set-Location)\s+(?:"[^"$`(]*"|'[^'$`(]*'|[^\s;&|$`"'()]+)\s*(?:&&|;)\s*)?"""
    r"""(?:&\s*)?(?:"[^"$`(]*python[^"$`(]*"|'[^'$`(]*python[^'$`(]*'|[^\s"';&|=$`()]*python[^\s"';&|=$`()]*)"""
    r"""\s+-m\s+""",
    re.IGNORECASE,
)

_SHELL_CHAIN_RE = re.compile(
    r";|&&|(?<![>&])&(?![&>])|\|\||\||\n|`|\$\(|\b(run_check|attest|escalate)\b.*\b(python|sh|bash|pwsh|node)\b")
