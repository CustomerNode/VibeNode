-- Migration 004: Enable Row Level Security on all public tables
--
-- WHY: Without RLS, anyone with the project URL + anon key can read, modify,
-- and delete all data via Supabase's public PostgREST API.
--
-- IMPACT: The VibeNode server uses the service-role (secret) key, which
-- bypasses RLS entirely — so this change has zero effect on application
-- behavior. It only blocks unauthorized access through the public API.
--
-- Run this in the Supabase SQL Editor (Dashboard → SQL Editor → New query).

-- Enable RLS on every kanban table
ALTER TABLE schema_version      ENABLE ROW LEVEL SECURITY;
ALTER TABLE preferences          ENABLE ROW LEVEL SECURITY;
ALTER TABLE board_columns        ENABLE ROW LEVEL SECURITY;
ALTER TABLE tasks                ENABLE ROW LEVEL SECURITY;
ALTER TABLE task_sessions        ENABLE ROW LEVEL SECURITY;
ALTER TABLE task_issues          ENABLE ROW LEVEL SECURITY;
ALTER TABLE task_status_history  ENABLE ROW LEVEL SECURITY;
ALTER TABLE task_tags            ENABLE ROW LEVEL SECURITY;

-- With RLS enabled and NO policies defined, the anon/public role gets
-- zero access. The service-role key used by the server bypasses RLS,
-- so the application continues to work exactly as before.
--
-- If you later need anon access to specific tables, create explicit
-- policies, e.g.:
--   CREATE POLICY "allow_anon_read" ON tasks
--     FOR SELECT USING (true);
