-- Add session_type column to task_sessions
-- Values: 'session' (default, work session) or 'planner' (AI planner)
ALTER TABLE task_sessions ADD COLUMN session_type TEXT NOT NULL DEFAULT 'session';
