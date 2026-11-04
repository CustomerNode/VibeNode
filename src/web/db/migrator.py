"""Lossless transfer between KanbanRepository backends.

Supports bidirectional migration (SQLite ↔ Supabase) with rollback safety.
The old backend data is NEVER deleted — it stays as a backup.
"""


class MigrationError(Exception):
    """Raised when a migration fails verification."""
    pass


class BackendMigrator:
    """Lossless transfer between any two KanbanRepository backends."""

    def export_all(self, source):
        """Dump every table into a portable dict.  Order matters for FK integrity."""
        return {
            "preferences": source.get_all_preferences(),
            "board_columns": source.get_all_columns_all_projects(),
            "tasks": source.get_all_tasks_ordered(),
            "task_sessions": source.get_all_task_sessions(),
            "task_tags": source.get_all_task_tags(),
            "status_history": source.get_all_status_history(),
        }

    def import_all(self, target, data):
        """Write into target.  Topological order ensures FK constraints pass."""
        # Preferences first (no FKs)
        for pref in data.get("preferences", []):
            target.set_preference(pref["key"], pref["value"])

        # Columns before tasks (no FKs but logically first)
        for col in data.get("board_columns", []):
            target.create_column(col)

        # Tasks in depth-first order (parents first) so parent_id FK resolves
        for task in data.get("tasks", []):
            target.create_task_from_dict(task)

        # Link tables after all tasks exist
        for ts in data.get("task_sessions", []):
            target.link_session(ts["task_id"], ts["session_id"])

        for tt in data.get("task_tags", []):
            target.add_tag(tt["task_id"], tt["tag"])

        for sh in data.get("status_history", []):
            target.add_status_history(
                sh["task_id"],
                sh.get("old_status"),
                sh["new_status"],
                sh.get("changed_by"),
                sh.get("changed_at"),
                session_id=sh.get("session_id"),
            )

    def switch_backend(self, current, target):
        """Full backend switch with rollback safety.

        Returns True on success.  Raises MigrationError if verification fails.
        The old backend data is NEVER deleted.
        """
        # Step 1: Export from current
        data = self.export_all(current)
        record_count = sum(
            len(v) for v in data.values() if isinstance(v, list)
        )

        # Step 2: Initialize target (creates schema if needed)
        target.initialize()

        # Step 3: Wipe target so it's a clean slate (no stale data)
        target.clear_all_data()

        # Step 4: Import into target
        self.import_all(target, data)

        # Step 4: Verify counts match
        verify = self.export_all(target)
        verify_count = sum(
            len(v) for v in verify.values() if isinstance(v, list)
        )

        if verify_count != record_count:
            raise MigrationError(
                f"Verification failed: {record_count} records exported, "
                f"only {verify_count} found in target. Old backend untouched."
            )

        # Step 5: Only now declare success
        # The old backend data stays in place as a backup
        return True
