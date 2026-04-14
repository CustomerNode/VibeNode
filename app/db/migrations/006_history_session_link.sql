-- Add optional session_id column to task_status_history
-- Links status transitions to the session that triggered them
ALTER TABLE task_status_history ADD COLUMN session_id TEXT;
