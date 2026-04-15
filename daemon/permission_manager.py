"""
PermissionManager -- permission policy storage, auto-approval logic, and
dangerous command detection.

Extracted from SessionManager (Phase 3 OOP decomposition).
"""

import json
import logging
import re
import threading
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class PermissionManager:
    """Permission policy storage, auto-approval logic, and dangerous command detection."""

    # ------------------------------------------------------------------
    # Dangerous-command detection (for "Almost Always")
    # ------------------------------------------------------------------

    # Only block IRREVERSIBLE or HARD-TO-REPAIR actions.
    # Recoverable operations (process kills, chmod, mv /tmp, docker rm,
    # git branch -D with reflog) are left alone to keep prompts minimal.
    _DANGEROUS_PATTERNS = [
        # ── Permanent file/data destruction ──
        r'\brm\s+.*-[rRf]',              # rm -r, rm -rf, rm -f
        r'\brm\s+.*\*',                   # rm with wildcards
        r'\bfind\b.*\s-delete\b',         # find ... -delete
        r'\bfind\b.*-exec\s+rm\b',       # find ... -exec rm
        r'\bshutil\.rmtree\b',           # Python rmtree in inline scripts
        r'>\s*/dev/',                      # redirect to devices
        r'^\s*>\s*[\'"]?/',               # bare redirect truncating a file
        r'\btruncate\s',                  # truncate command
        r'\bmkfs\b',                      # format filesystem
        r'\bdd\s+if=',                    # dd disk overwrite
        r'\bmv\s+.*\s+/dev/null\b',       # mv to /dev/null (data gone)

        # ── Git operations that rewrite shared history ──
        r'\bgit\s+push\s+.*--force',      # force push (overwrites remote)
        r'\bgit\s+push\s+-f\b',          # force push short flag
        r'\bgit\s+reset\s+--hard',        # hard reset (uncommitted work gone)
        r'\bgit\s+clean\s+-[fdxe]',       # git clean (untracked files gone forever)
        r'\bgit\s+stash\s+clear\b',       # clear ALL stashes

        # ── SQL irreversible operations ──
        r'\bDROP\s+(TABLE|DATABASE|SCHEMA|VIEW)',
        r'\bTRUNCATE\b',

        # ── Public/irreversible deployment ──
        r'\bnpm\s+publish\b',            # publishes to the world, can't unpublish

        # ── Remote code execution (unknown impact) ──
        r'\bcurl\b.*\|\s*(ba)?sh',        # pipe curl to shell
        r'\bwget\b.*\|\s*(ba)?sh',        # pipe wget to shell
        r'\bpython[3]?\s+-c\s+.*\brmtree\b',  # python -c with rmtree
    ]
    _DANGEROUS_RE = None  # lazily compiled

    def __init__(self, emit_entry_fn=None):
        """Initialize the PermissionManager.

        Args:
            emit_entry_fn: Optional callback for pushing audit log entries
                to connected WebSocket clients.  Signature:
                ``(session_id: str, entry: LogEntry, index: int) -> None``.
                Set by SessionManager after construction.  If None, audit
                entries are still appended to the session info but not
                pushed to the UI in real time.
        """
        # Permission policy (synced from browser) — persisted to disk
        self._policy_path = Path.home() / ".claude" / "gui_permission_policy.json"
        self._permission_policy, self._custom_rules = self._load_policy()
        # UI preferences (send behavior, etc.) — persisted to disk
        self._ui_prefs_path = Path.home() / ".claude" / "gui_ui_prefs.json"
        self._ui_prefs = self._load_ui_prefs()
        # Callback for emitting log entries (set by SessionManager)
        self._emit_entry_fn = emit_entry_fn

    # ------------------------------------------------------------------
    # Permission policy persistence
    # ------------------------------------------------------------------

    def _load_policy(self):
        """Load persisted permission policy from disk."""
        try:
            if self._policy_path.exists():
                data = json.loads(self._policy_path.read_text())
                policy = data.get("policy", "manual")
                if policy in ("manual", "auto", "almost_always", "custom"):
                    logger.info("Loaded persisted permission policy: %s", policy)
                    return policy, data.get("custom_rules", {})
        except Exception as e:
            logger.warning("Failed to load permission policy: %s", e)
        return "manual", {}

    def _save_policy(self):
        """Persist permission policy to disk."""
        try:
            self._policy_path.parent.mkdir(parents=True, exist_ok=True)
            self._policy_path.write_text(json.dumps({
                "policy": self._permission_policy,
                "custom_rules": self._custom_rules,
            }))
        except Exception as e:
            logger.warning("Failed to save permission policy: %s", e)

    def get_permission_policy(self) -> dict:
        """Return the current permission policy and custom rules."""
        return {
            "policy": self._permission_policy,
            "custom_rules": self._custom_rules,
        }

    def set_permission_policy(self, policy: str, custom_rules: dict = None) -> None:
        """Update the permission policy (synced from browser)."""
        if policy not in ("manual", "auto", "almost_always", "custom"):
            return
        self._permission_policy = policy
        self._custom_rules = custom_rules or {}
        self._save_policy()
        logger.info("Permission policy updated and saved: %s", policy)

    # ------------------------------------------------------------------
    # UI Preferences persistence
    # ------------------------------------------------------------------

    def _load_ui_prefs(self) -> dict:
        """Load persisted UI preferences from disk."""
        try:
            if self._ui_prefs_path.exists():
                data = json.loads(self._ui_prefs_path.read_text())
                if isinstance(data, dict):
                    logger.info("Loaded persisted UI prefs: %s", list(data.keys()))
                    return data
        except Exception as e:
            logger.warning("Failed to load UI prefs: %s", e)
        return {}

    def _save_ui_prefs(self):
        """Persist UI preferences to disk."""
        try:
            self._ui_prefs_path.parent.mkdir(parents=True, exist_ok=True)
            self._ui_prefs_path.write_text(json.dumps(self._ui_prefs))
        except Exception as e:
            logger.warning("Failed to save UI prefs: %s", e)

    def get_ui_prefs(self) -> dict:
        """Return all persisted UI preferences."""
        return dict(self._ui_prefs)

    def set_ui_prefs(self, prefs: dict) -> None:
        """Merge new preferences into saved UI prefs and persist."""
        if not isinstance(prefs, dict):
            return
        self._ui_prefs.update(prefs)
        self._save_ui_prefs()
        logger.info("UI prefs updated and saved: %s", list(prefs.keys()))

    # ------------------------------------------------------------------
    # Auto-approval logic
    # ------------------------------------------------------------------

    def should_auto_approve(self, tool_name: str, tool_input: dict) -> bool:
        """Check if a tool use should be auto-approved based on the current policy."""
        policy = self._permission_policy

        if policy == "manual":
            return False
        if policy == "auto":
            return True
        if policy == "almost_always":
            # Auto-approve everything EXCEPT dangerous commands
            if self.is_dangerous(tool_name, tool_input):
                return False
            return True
        if policy == "custom":
            rules = self._custom_rules
            tool_lower = (tool_name or "").lower()

            # Each custom rule is evaluated independently.  Rules are OR'd:
            # if ANY rule matches, the tool is auto-approved.  The evaluation
            # order matches the UI checkbox order for consistency.

            # Rule 1: Auto-approve all file reads (safe -- no data modified)
            if rules.get("approveAllReads") and tool_lower == "read":
                return True
            # Rule 2: Auto-approve project-scoped reads (client-side scope
            # check -- server just checks the flag)
            if rules.get("approveProjectReads") and tool_lower == "read":
                return True
            # Rule 3: Auto-approve all bash commands (risky -- user accepts
            # full responsibility for shell command safety)
            if rules.get("approveAllBash") and tool_lower == "bash":
                return True
            # Rule 4: Auto-approve file writes and edits (moderate risk --
            # files can be reverted via git)
            if rules.get("approveProjectWrites") and tool_lower in ("write", "edit"):
                return True
            # Rule 5: Auto-approve glob searches (safe -- read-only)
            if rules.get("approveGlob") and tool_lower == "glob":
                return True
            # Rule 6: Auto-approve grep searches (safe -- read-only)
            if rules.get("approveGrep") and tool_lower == "grep":
                return True

            # Rule 7: Custom regex pattern.  Builds a "question" string
            # that mirrors what the frontend shows the user, then matches
            # the user's regex against it.  This allows arbitrary approval
            # rules (e.g. "pytest" to auto-approve all test runs).
            custom_pattern = rules.get("customPattern", "")
            if custom_pattern:
                try:
                    desc = ""
                    if isinstance(tool_input, dict):
                        desc = tool_input.get("command", "") or tool_input.get("file_path", "") or tool_input.get("path", "") or tool_input.get("pattern", "")
                    question = f"Claude wants to use {tool_name}:\n\n{desc}"
                    if re.search(custom_pattern, question, re.IGNORECASE):
                        return True
                except re.error:
                    # Invalid regex -- silently ignore rather than crashing
                    # the permission flow.  The user will see that their
                    # pattern isn't matching and can fix it in the UI.
                    pass

        return False

    # ------------------------------------------------------------------
    # Dangerous-command detection (for "Almost Always")
    # ------------------------------------------------------------------

    @classmethod
    def is_dangerous(cls, tool_name: str, tool_input) -> bool:
        """Return True if tool_input looks destructive (used by Almost Always)."""
        if (tool_name or "").lower() != "bash":
            return False
        command = ""
        if isinstance(tool_input, dict):
            command = tool_input.get("command", "")
        if not command:
            return False
        if cls._DANGEROUS_RE is None:
            cls._DANGEROUS_RE = re.compile(
                "|".join(cls._DANGEROUS_PATTERNS), re.IGNORECASE | re.MULTILINE
            )
        return bool(cls._DANGEROUS_RE.search(command))

    # ------------------------------------------------------------------
    # Audit logging
    # ------------------------------------------------------------------

    def log_auto_approved(self, session_id: str, info, tool_name: str,
                          tool_input, policy: str) -> None:
        """Log an audit entry when a tool is auto-approved (or blocked).

        This must never raise — a logging failure should not break the
        permission callback that auto-approved the tool.
        """
        try:
            desc = ""
            if isinstance(tool_input, dict):
                desc = (tool_input.get("command", "")
                        or tool_input.get("file_path", "")
                        or tool_input.get("path", "")
                        or tool_input.get("pattern", ""))
            if policy == "almost-always-blocked":
                text = f"Dangerous command blocked by Almost Always — prompting for manual approval\n{tool_name}: {desc}"
                is_error = True
            else:
                text = f"Auto-approved ({policy})\n{tool_name}: {desc}"
                is_error = False

            # Import LogEntry here to avoid circular imports at module level
            from daemon.session_manager import LogEntry
            entry = LogEntry(kind="permission", text=text, name=tool_name, is_error=is_error)
            with info._lock:
                info.entries.append(entry)
                idx = len(info.entries) - 1
            if self._emit_entry_fn:
                self._emit_entry_fn(session_id, entry, idx)
        except Exception as e:
            logger.warning("Failed to log auto-approved permission: %s", e)
