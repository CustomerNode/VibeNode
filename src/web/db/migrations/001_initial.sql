-- Migration 001: Initial Kanban schema

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS preferences (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

INSERT OR IGNORE INTO preferences (key, value, updated_at) VALUES
    ('kanban_backend',      'sqlite',  datetime('now')),
    ('kanban_auto_advance', 'false',   datetime('now')),
    ('kanban_page_size',    '50',      datetime('now'));

CREATE TABLE IF NOT EXISTS board_columns (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    name TEXT NOT NULL,
    status_key TEXT NOT NULL,
    position INTEGER NOT NULL DEFAULT 0,
    color TEXT DEFAULT '#8b949e',
    is_terminal INTEGER DEFAULT 0,
    is_regression INTEGER DEFAULT 0,
    sort_mode TEXT DEFAULT 'manual',
    sort_direction TEXT DEFAULT 'desc',
    UNIQUE(project_id, status_key)
);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    parent_id TEXT REFERENCES tasks(id) ON DELETE CASCADE,
    position INTEGER NOT NULL DEFAULT 0,
    title TEXT NOT NULL,
    description TEXT,
    verification_url TEXT,
    status TEXT NOT NULL DEFAULT 'not_started',
    owner TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project_id);
CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks(parent_id);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(project_id, status);
CREATE INDEX IF NOT EXISTS idx_tasks_position ON tasks(project_id, status, position);
CREATE INDEX IF NOT EXISTS idx_tasks_owner ON tasks(owner);

CREATE TABLE IF NOT EXISTS task_sessions (
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    session_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (task_id, session_id)
);

CREATE TABLE IF NOT EXISTS task_issues (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    description TEXT NOT NULL,
    session_id TEXT,
    resolved_at TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_issues_task ON task_issues(task_id);

CREATE TABLE IF NOT EXISTS task_status_history (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    old_status TEXT,
    new_status TEXT NOT NULL,
    changed_by TEXT,
    changed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_status_hist_task ON task_status_history(task_id);
CREATE INDEX IF NOT EXISTS idx_status_hist_time ON task_status_history(changed_at);

CREATE TABLE IF NOT EXISTS tags (
    tag         TEXT NOT NULL,
    project_id  TEXT NOT NULL,
    color       TEXT,
    usage_count INTEGER DEFAULT 0,
    PRIMARY KEY (tag, project_id)
);
CREATE INDEX IF NOT EXISTS idx_tags_project ON tags(project_id);

CREATE TABLE IF NOT EXISTS task_tags (
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    tag TEXT NOT NULL,
    PRIMARY KEY (task_id, tag)
);
CREATE INDEX IF NOT EXISTS idx_task_tags_tag ON task_tags(tag);

INSERT INTO schema_version (version, applied_at)
VALUES (1, datetime('now'));
