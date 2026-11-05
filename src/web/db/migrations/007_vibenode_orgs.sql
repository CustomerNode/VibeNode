-- Migration 007: VibeNode Orgs and Phone a Friend.
--
-- An org is a group of people; Phone a Friend lets one member open ONE of
-- their sessions to another member, one grant at a time, until it expires or
-- is revoked.
--
-- Access model. Every table has Row Level Security on and NO policies, so the
-- public (publishable-key) role reads and writes nothing directly. Members act
-- only through the functions below, each taking the member's own secret token
-- (only its SHA-256 is stored) and checking what that member may do. The
-- functions run as their owner (SECURITY DEFINER) with a fixed search_path.
-- Creating an org needs the project's secret key (orgs_create is granted to
-- service_role only), so only someone with this cloud storage can start one.
-- An invite code is single-use and expires; redeeming it creates a member and
-- returns that member's token, once.
--
-- Run in the Supabase SQL Editor, or let VibeNode apply it during Orgs setup.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS orgs (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 80),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS org_members (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id      UUID NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    username    TEXT NOT NULL CHECK (username ~ '^[A-Za-z0-9][A-Za-z0-9 ._-]{0,31}$'),
    role        TEXT NOT NULL DEFAULT 'member' CHECK (role IN ('owner', 'admin', 'member')),
    token_hash  TEXT NOT NULL UNIQUE,
    invited_by  UUID REFERENCES org_members(id) ON DELETE SET NULL,
    removed_at  TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS org_members_name
    ON org_members (org_id, lower(username)) WHERE removed_at IS NULL;

CREATE TABLE IF NOT EXISTS org_invites (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id      UUID NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    code_hash   TEXT NOT NULL UNIQUE,
    created_by  UUID NOT NULL REFERENCES org_members(id) ON DELETE CASCADE,
    expires_at  TIMESTAMPTZ NOT NULL,
    used_at     TIMESTAMPTZ,
    used_by     UUID REFERENCES org_members(id) ON DELETE SET NULL,
    revoked_at  TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS pf_shared_sessions (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id      UUID NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    owner_id    UUID NOT NULL REFERENCES org_members(id) ON DELETE CASCADE,
    source      TEXT NOT NULL CHECK (length(source) <= 120),
    session_id  TEXT NOT NULL CHECK (length(session_id) <= 120),
    title       TEXT NOT NULL DEFAULT '' CHECK (length(title) <= 200),
    status      TEXT NOT NULL DEFAULT 'idle' CHECK (length(status) <= 40),
    last_seq    BIGINT NOT NULL DEFAULT 0,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (owner_id, source, session_id)
);

CREATE TABLE IF NOT EXISTS pf_grants (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    share_id    UUID NOT NULL REFERENCES pf_shared_sessions(id) ON DELETE CASCADE,
    guest_id    UUID NOT NULL REFERENCES org_members(id) ON DELETE CASCADE,
    level       TEXT NOT NULL CHECK (level IN ('view', 'suggest', 'drive')),
    note        TEXT NOT NULL DEFAULT '' CHECK (length(note) <= 500),
    expires_at  TIMESTAMPTZ,
    revoked_at  TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS pf_grants_guest ON pf_grants (guest_id, created_at);

CREATE TABLE IF NOT EXISTS pf_posts (
    share_id    UUID NOT NULL REFERENCES pf_shared_sessions(id) ON DELETE CASCADE,
    seq         BIGINT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('entry', 'guest_msg', 'status')),
    author_id   UUID NOT NULL REFERENCES org_members(id) ON DELETE CASCADE,
    grant_id    UUID REFERENCES pf_grants(id) ON DELETE SET NULL,
    body        JSONB NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (share_id, seq),
    CHECK (octet_length(body::text) <= 65536)
);

ALTER TABLE orgs               ENABLE ROW LEVEL SECURITY;
ALTER TABLE org_members        ENABLE ROW LEVEL SECURITY;
ALTER TABLE org_invites        ENABLE ROW LEVEL SECURITY;
ALTER TABLE pf_shared_sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE pf_grants          ENABLE ROW LEVEL SECURITY;
ALTER TABLE pf_posts           ENABLE ROW LEVEL SECURITY;

-- ── helpers (not callable from outside) ─────────────────────────────────────

CREATE OR REPLACE FUNCTION orgs_hash(p TEXT) RETURNS TEXT
LANGUAGE sql IMMUTABLE SET search_path = public, extensions AS $$
    SELECT encode(digest(coalesce(p, ''), 'sha256'), 'hex')
$$;

CREATE OR REPLACE FUNCTION orgs_new_secret() RETURNS TEXT
LANGUAGE sql VOLATILE SET search_path = public, extensions AS $$
    SELECT translate(encode(gen_random_bytes(32), 'base64'), '+/=', '-_')
$$;

-- The member a token belongs to, or an error. Removed members have no access.
CREATE OR REPLACE FUNCTION orgs_member(p_token TEXT) RETURNS org_members
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = public, extensions AS $$
DECLARE m org_members;
BEGIN
    IF p_token IS NULL OR length(p_token) < 20 THEN
        RAISE EXCEPTION 'not_a_member' USING ERRCODE = '42501';
    END IF;
    SELECT * INTO m FROM org_members
     WHERE token_hash = orgs_hash(p_token) AND removed_at IS NULL;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'not_a_member' USING ERRCODE = '42501';
    END IF;
    RETURN m;
END $$;

CREATE OR REPLACE FUNCTION pf_grant_live(g pf_grants) RETURNS BOOLEAN
LANGUAGE sql STABLE AS $$
    SELECT g.revoked_at IS NULL AND (g.expires_at IS NULL OR g.expires_at > now())
$$;

-- ── orgs ────────────────────────────────────────────────────────────────────

-- Secret key only: start an org and its first member (the owner).
CREATE OR REPLACE FUNCTION orgs_create(p_name TEXT, p_username TEXT) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, extensions AS $$
DECLARE o orgs; tok TEXT := orgs_new_secret(); mid UUID;
BEGIN
    INSERT INTO orgs (name) VALUES (trim(p_name)) RETURNING * INTO o;
    INSERT INTO org_members (org_id, username, role, token_hash)
         VALUES (o.id, trim(p_username), 'owner', orgs_hash(tok)) RETURNING id INTO mid;
    RETURN jsonb_build_object('org_id', o.id, 'org_name', o.name, 'member_id', mid,
                              'username', trim(p_username), 'role', 'owner', 'token', tok);
END $$;

CREATE OR REPLACE FUNCTION orgs_me(p_token TEXT) RETURNS JSONB
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = public, extensions AS $$
DECLARE m org_members := orgs_member(p_token); o orgs;
BEGIN
    SELECT * INTO o FROM orgs WHERE id = m.org_id;
    RETURN jsonb_build_object('org_id', o.id, 'org_name', o.name, 'member_id', m.id,
                              'username', m.username, 'role', m.role);
END $$;

CREATE OR REPLACE FUNCTION orgs_members(p_token TEXT) RETURNS JSONB
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = public, extensions AS $$
DECLARE m org_members := orgs_member(p_token);
BEGIN
    RETURN coalesce((SELECT jsonb_agg(jsonb_build_object(
                'member_id', x.id, 'username', x.username, 'role', x.role,
                'you', x.id = m.id, 'joined_at', x.created_at) ORDER BY lower(x.username))
             FROM org_members x WHERE x.org_id = m.org_id AND x.removed_at IS NULL), '[]'::jsonb);
END $$;

-- Owner or admin: a single-use invite code that expires in p_days (1..30).
CREATE OR REPLACE FUNCTION orgs_create_invite(p_token TEXT, p_days INT) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, extensions AS $$
DECLARE m org_members := orgs_member(p_token); code TEXT := orgs_new_secret(); exp TIMESTAMPTZ;
BEGIN
    IF m.role NOT IN ('owner', 'admin') THEN
        RAISE EXCEPTION 'not_allowed' USING ERRCODE = '42501';
    END IF;
    IF p_days IS NULL OR p_days < 1 OR p_days > 30 THEN
        RAISE EXCEPTION 'bad_days' USING ERRCODE = '22023';
    END IF;
    exp := now() + make_interval(days => p_days);
    INSERT INTO org_invites (org_id, code_hash, created_by, expires_at)
         VALUES (m.org_id, orgs_hash(code), m.id, exp);
    RETURN jsonb_build_object('code', code, 'expires_at', exp);
END $$;

-- Anyone holding a code: join once. The code stops working the moment it is used.
CREATE OR REPLACE FUNCTION orgs_redeem_invite(p_code TEXT, p_username TEXT) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, extensions AS $$
DECLARE i org_invites; o orgs; tok TEXT := orgs_new_secret(); mid UUID;
BEGIN
    SELECT * INTO i FROM org_invites WHERE code_hash = orgs_hash(p_code) FOR UPDATE;
    IF NOT FOUND OR i.used_at IS NOT NULL OR i.revoked_at IS NOT NULL OR i.expires_at <= now() THEN
        RAISE EXCEPTION 'invite_invalid' USING ERRCODE = '42501';
    END IF;
    BEGIN
        INSERT INTO org_members (org_id, username, role, token_hash, invited_by)
             VALUES (i.org_id, trim(p_username), 'member', orgs_hash(tok), i.created_by)
          RETURNING id INTO mid;
    EXCEPTION WHEN unique_violation THEN
        RAISE EXCEPTION 'username_taken' USING ERRCODE = '23505';
    END;
    UPDATE org_invites SET used_at = now(), used_by = mid WHERE id = i.id;
    SELECT * INTO o FROM orgs WHERE id = i.org_id;
    RETURN jsonb_build_object('org_id', o.id, 'org_name', o.name, 'member_id', mid,
                              'username', trim(p_username), 'role', 'member', 'token', tok);
END $$;

CREATE OR REPLACE FUNCTION orgs_revoke_invites(p_token TEXT) RETURNS INT
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, extensions AS $$
DECLARE m org_members := orgs_member(p_token); n INT;
BEGIN
    IF m.role NOT IN ('owner', 'admin') THEN
        RAISE EXCEPTION 'not_allowed' USING ERRCODE = '42501';
    END IF;
    UPDATE org_invites SET revoked_at = now()
     WHERE org_id = m.org_id AND used_at IS NULL AND revoked_at IS NULL;
    GET DIAGNOSTICS n = ROW_COUNT;
    RETURN n;
END $$;

-- Owner or admin removes a member (never the owner); every grant they gave or
-- hold ends at once. A member may also leave by naming themselves.
CREATE OR REPLACE FUNCTION orgs_remove_member(p_token TEXT, p_member UUID) RETURNS BOOLEAN
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, extensions AS $$
DECLARE m org_members := orgs_member(p_token); t org_members;
BEGIN
    SELECT * INTO t FROM org_members WHERE id = p_member AND org_id = m.org_id AND removed_at IS NULL;
    IF NOT FOUND THEN RETURN FALSE; END IF;
    IF t.role = 'owner' OR (t.id <> m.id AND m.role NOT IN ('owner', 'admin')) THEN
        RAISE EXCEPTION 'not_allowed' USING ERRCODE = '42501';
    END IF;
    UPDATE org_members SET removed_at = now() WHERE id = t.id;
    UPDATE pf_grants g SET revoked_at = now()
      FROM pf_shared_sessions s
     WHERE g.share_id = s.id AND g.revoked_at IS NULL AND (g.guest_id = t.id OR s.owner_id = t.id);
    RETURN TRUE;
END $$;

-- ── Phone a Friend ──────────────────────────────────────────────────────────

-- The project a shared session belongs to on its owner's computer, so a
-- friend always sees which project they were phoned about.
ALTER TABLE pf_shared_sessions ADD COLUMN IF NOT EXISTS project TEXT NOT NULL DEFAULT '';

-- The call's note: written when phoning, editable afterwards by either side.
ALTER TABLE pf_grants ADD COLUMN IF NOT EXISTS note_by UUID REFERENCES org_members(id) ON DELETE SET NULL;
ALTER TABLE pf_grants ADD COLUMN IF NOT EXISTS note_at TIMESTAMPTZ;

CREATE OR REPLACE FUNCTION pf_grant_json(g pf_grants, s pf_shared_sessions) RETURNS JSONB
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, extensions AS $$
    SELECT jsonb_build_object(
        'grant_id', g.id, 'share_id', s.id, 'session_id', s.session_id, 'source', s.source,
        'title', s.title, 'project', s.project, 'status', s.status, 'last_seq', s.last_seq, 'level', g.level,
        'note', g.note, 'expires_at', g.expires_at, 'revoked_at', g.revoked_at,
        'created_at', g.created_at, 'live', pf_grant_live(g),
        'owner_id', s.owner_id, 'owner', (SELECT username FROM org_members WHERE id = s.owner_id),
        'guest_id', g.guest_id, 'guest', (SELECT username FROM org_members WHERE id = g.guest_id),
        'note_at', coalesce(g.note_at, g.created_at),
        'note_by', (SELECT username FROM org_members WHERE id = coalesce(g.note_by, s.owner_id)))
$$;

-- Open one of your sessions to one member. p_minutes NULL = until revoked.
-- p_project has a default so a VibeNode that does not send it still calls
-- this one function (the eight-argument version it replaced is dropped).
DROP FUNCTION IF EXISTS pf_share(TEXT, UUID, TEXT, TEXT, TEXT, TEXT, INT, TEXT);
CREATE OR REPLACE FUNCTION pf_share(p_token TEXT, p_guest UUID, p_source TEXT, p_session TEXT,
                                    p_title TEXT, p_level TEXT, p_minutes INT, p_note TEXT,
                                    p_project TEXT DEFAULT '')
RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, extensions AS $$
DECLARE m org_members := orgs_member(p_token); s pf_shared_sessions; g pf_grants;
BEGIN
    IF p_guest = m.id OR NOT EXISTS (SELECT 1 FROM org_members
            WHERE id = p_guest AND org_id = m.org_id AND removed_at IS NULL) THEN
        RAISE EXCEPTION 'not_a_member_of_your_org' USING ERRCODE = '42501';
    END IF;
    IF p_minutes IS NOT NULL AND (p_minutes < 1 OR p_minutes > 60 * 24 * 90) THEN
        RAISE EXCEPTION 'bad_minutes' USING ERRCODE = '22023';
    END IF;
    INSERT INTO pf_shared_sessions (org_id, owner_id, source, session_id, title, project)
         VALUES (m.org_id, m.id, p_source, p_session, coalesce(p_title, ''), left(coalesce(p_project, ''), 120))
    ON CONFLICT (owner_id, source, session_id) DO UPDATE
        SET title = EXCLUDED.title,
            project = CASE WHEN EXCLUDED.project <> '' THEN EXCLUDED.project ELSE pf_shared_sessions.project END
      RETURNING * INTO s;
    -- One live grant per person per session: a second call changes it.
    UPDATE pf_grants SET revoked_at = now()
     WHERE share_id = s.id AND guest_id = p_guest AND revoked_at IS NULL;
    INSERT INTO pf_grants (share_id, guest_id, level, note, expires_at)
         VALUES (s.id, p_guest, p_level, coalesce(p_note, ''),
                 CASE WHEN p_minutes IS NULL THEN NULL ELSE now() + make_interval(mins => p_minutes) END)
      RETURNING * INTO g;
    RETURN pf_grant_json(g, s);
END $$;

CREATE OR REPLACE FUNCTION pf_revoke(p_token TEXT, p_grant UUID) RETURNS BOOLEAN
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, extensions AS $$
DECLARE m org_members := orgs_member(p_token); n INT;
BEGIN
    UPDATE pf_grants g SET revoked_at = now()
      FROM pf_shared_sessions s
     WHERE g.id = p_grant AND g.share_id = s.id AND g.revoked_at IS NULL
       AND (s.owner_id = m.id OR g.guest_id = m.id);
    GET DIAGNOSTICS n = ROW_COUNT;
    RETURN n > 0;
END $$;

-- Change a call's note. Either side of the call may, live or ended, so the
-- note can record how it went.
CREATE OR REPLACE FUNCTION pf_set_note(p_token TEXT, p_grant UUID, p_note TEXT) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, extensions AS $$
DECLARE m org_members := orgs_member(p_token); g pf_grants; s pf_shared_sessions;
BEGIN
    IF p_note IS NULL OR length(p_note) > 500 THEN
        RAISE EXCEPTION 'bad_text' USING ERRCODE = '22023';
    END IF;
    SELECT * INTO g FROM pf_grants WHERE id = p_grant FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'no_access' USING ERRCODE = '42501'; END IF;
    SELECT * INTO s FROM pf_shared_sessions WHERE id = g.share_id;
    IF NOT (s.owner_id = m.id OR g.guest_id = m.id) THEN
        RAISE EXCEPTION 'no_access' USING ERRCODE = '42501';
    END IF;
    UPDATE pf_grants SET note = p_note, note_by = m.id, note_at = now() WHERE id = g.id RETURNING * INTO g;
    RETURN pf_grant_json(g, s);
END $$;

-- Grants you gave (p_side 'out') or hold ('in'), live ones and those that
-- ended in the last p_days days.
CREATE OR REPLACE FUNCTION pf_grants_list(p_token TEXT, p_side TEXT, p_days INT) RETURNS JSONB
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = public, extensions AS $$
DECLARE m org_members := orgs_member(p_token);
BEGIN
    RETURN coalesce((SELECT jsonb_agg(pf_grant_json(g, s) ORDER BY g.created_at DESC)
      FROM pf_grants g JOIN pf_shared_sessions s ON s.id = g.share_id
     WHERE (CASE WHEN p_side = 'in' THEN g.guest_id = m.id ELSE s.owner_id = m.id END)
       AND (pf_grant_live(g) OR g.created_at > now() - make_interval(days => greatest(coalesce(p_days, 7), 0)))
    ), '[]'::jsonb);
END $$;

-- The sharer's VibeNode posts the thread: p_posts is a list of
-- {kind: 'entry'|'status', body}. Sequence numbers are assigned here.
CREATE OR REPLACE FUNCTION pf_post(p_token TEXT, p_share UUID, p_posts JSONB, p_status TEXT)
RETURNS BIGINT
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, extensions AS $$
DECLARE m org_members := orgs_member(p_token); s pf_shared_sessions; p JSONB; n BIGINT;
BEGIN
    SELECT * INTO s FROM pf_shared_sessions WHERE id = p_share AND owner_id = m.id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'not_your_session' USING ERRCODE = '42501'; END IF;
    n := s.last_seq;
    IF jsonb_typeof(p_posts) = 'array' THEN
        IF jsonb_array_length(p_posts) > 200 THEN
            RAISE EXCEPTION 'too_many' USING ERRCODE = '22023';
        END IF;
        FOR p IN SELECT * FROM jsonb_array_elements(p_posts) LOOP
            IF p->>'kind' NOT IN ('entry', 'status') THEN
                RAISE EXCEPTION 'bad_kind' USING ERRCODE = '22023';
            END IF;
            n := n + 1;
            INSERT INTO pf_posts (share_id, seq, kind, author_id, body)
                 VALUES (s.id, n, p->>'kind', m.id, coalesce(p->'body', '{}'::jsonb));
        END LOOP;
    END IF;
    UPDATE pf_shared_sessions SET last_seq = n, status = coalesce(p_status, status) WHERE id = s.id;
    RETURN n;
END $$;

-- Read a shared thread: its owner, or a guest with a live grant.
CREATE OR REPLACE FUNCTION pf_read(p_token TEXT, p_share UUID, p_after BIGINT, p_limit INT,
                                   p_before BIGINT)
RETURNS JSONB
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = public, extensions AS $$
DECLARE m org_members := orgs_member(p_token); s pf_shared_sessions; lim INT;
BEGIN
    SELECT * INTO s FROM pf_shared_sessions WHERE id = p_share;
    IF NOT FOUND OR NOT (s.owner_id = m.id OR EXISTS (
            SELECT 1 FROM pf_grants g WHERE g.share_id = s.id AND g.guest_id = m.id AND pf_grant_live(g))) THEN
        RAISE EXCEPTION 'no_access' USING ERRCODE = '42501';
    END IF;
    lim := least(greatest(coalesce(p_limit, 100), 1), 500);
    RETURN jsonb_build_object('status', s.status, 'last_seq', s.last_seq, 'title', s.title,
        'posts', coalesce((SELECT jsonb_agg(x ORDER BY x.seq) FROM (
            SELECT p.seq, p.kind, p.body, p.created_at,
                   (SELECT username FROM org_members WHERE id = p.author_id) AS author
              FROM pf_posts p
             WHERE p.share_id = s.id AND p.seq > coalesce(p_after, 0)
               AND (p_before IS NULL OR p.seq < p_before)
             ORDER BY CASE WHEN p_before IS NULL THEN p.seq END ASC,
                      CASE WHEN p_before IS NOT NULL THEN p.seq END DESC
             LIMIT lim) x), '[]'::jsonb));
END $$;

-- A guest writes into the thread (Suggest or Drive grants only).
CREATE OR REPLACE FUNCTION pf_guest_message(p_token TEXT, p_grant UUID, p_text TEXT) RETURNS BIGINT
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, extensions AS $$
DECLARE m org_members := orgs_member(p_token); g pf_grants; s pf_shared_sessions; n BIGINT;
BEGIN
    SELECT * INTO g FROM pf_grants WHERE id = p_grant AND guest_id = m.id;
    IF NOT FOUND OR NOT pf_grant_live(g) OR g.level NOT IN ('suggest', 'drive') THEN
        RAISE EXCEPTION 'no_access' USING ERRCODE = '42501';
    END IF;
    IF p_text IS NULL OR length(p_text) = 0 OR length(p_text) > 20000 THEN
        RAISE EXCEPTION 'bad_text' USING ERRCODE = '22023';
    END IF;
    SELECT * INTO s FROM pf_shared_sessions WHERE id = g.share_id FOR UPDATE;
    n := s.last_seq + 1;
    INSERT INTO pf_posts (share_id, seq, kind, author_id, grant_id, body)
         VALUES (s.id, n, 'guest_msg', m.id, g.id,
                 jsonb_build_object('text', p_text, 'level', g.level));
    UPDATE pf_shared_sessions SET last_seq = n WHERE id = s.id;
    RETURN n;
END $$;

-- The sharer's VibeNode picks up guests' messages on its shared sessions.
-- Each comes with the grant's CURRENT level and whether it is still live, so
-- a message written before a revoke is never acted on.
CREATE OR REPLACE FUNCTION pf_inbox(p_token TEXT, p_since TIMESTAMPTZ) RETURNS JSONB
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = public, extensions AS $$
DECLARE m org_members := orgs_member(p_token);
BEGIN
    RETURN coalesce((SELECT jsonb_agg(jsonb_build_object(
            'share_id', s.id, 'session_id', s.session_id, 'source', s.source, 'seq', p.seq,
            'text', p.body->>'text', 'grant_id', g.id, 'level', g.level, 'live', pf_grant_live(g),
            'guest', (SELECT username FROM org_members WHERE id = p.author_id),
            'created_at', p.created_at) ORDER BY p.created_at, p.seq)
      FROM pf_posts p JOIN pf_shared_sessions s ON s.id = p.share_id
                      JOIN pf_grants g ON g.id = p.grant_id
     WHERE s.owner_id = m.id AND p.kind = 'guest_msg' AND p.created_at > coalesce(p_since, now() - interval '1 day')
    ), '[]'::jsonb);
END $$;

-- Everything a VibeNode needs in one round trip, called about once a second:
-- the calls it gave and holds, friends' new messages on its sessions, and the
-- new posts of each phoned session after the cursor it sends
-- (p_cursors: {share_id: last seq seen}; a session not in it starts at 0).
CREATE OR REPLACE FUNCTION pf_sync(p_token TEXT, p_cursors JSONB, p_inbox_since TIMESTAMPTZ, p_days INT)
RETURNS JSONB
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = public, extensions AS $$
DECLARE m org_members := orgs_member(p_token); posts JSONB := '{}'::jsonb; k TEXT; after BIGINT;
BEGIN
    -- Every phoned session with a live call to this member: its posts after
    -- the cursor sent for it (0 for one not seen yet).
    FOR k, after IN
        SELECT DISTINCT s.id::text, coalesce((p_cursors ->> s.id::text)::BIGINT, 0)
          FROM pf_shared_sessions s JOIN pf_grants g ON g.share_id = s.id
         WHERE g.guest_id = m.id AND pf_grant_live(g)
           AND s.last_seq > coalesce((p_cursors ->> s.id::text)::BIGINT, 0)
    LOOP
        posts := posts || jsonb_build_object(k, coalesce((SELECT jsonb_agg(x ORDER BY x.seq) FROM (
            SELECT p.seq, p.kind, p.body, p.created_at,
                   (SELECT username FROM org_members WHERE id = p.author_id) AS author
              FROM pf_posts p WHERE p.share_id::text = k AND p.seq > after
             ORDER BY p.seq LIMIT 300) x), '[]'::jsonb));
    END LOOP;
    RETURN jsonb_build_object(
        'in', pf_grants_list(p_token, 'in', p_days),
        'out', pf_grants_list(p_token, 'out', p_days),
        'inbox', pf_inbox(p_token, p_inbox_since),
        'posts', posts);
END $$;

-- ── who may call what ───────────────────────────────────────────────────────

REVOKE ALL ON ALL TABLES IN SCHEMA public FROM anon, authenticated;
REVOKE EXECUTE ON FUNCTION orgs_hash(TEXT), orgs_new_secret(), orgs_member(TEXT),
    pf_grant_live(pf_grants), pf_grant_json(pf_grants, pf_shared_sessions),
    orgs_create(TEXT, TEXT) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION orgs_create(TEXT, TEXT) TO service_role;
REVOKE EXECUTE ON FUNCTION orgs_me(TEXT), orgs_members(TEXT), orgs_create_invite(TEXT, INT),
    orgs_redeem_invite(TEXT, TEXT), orgs_revoke_invites(TEXT), orgs_remove_member(TEXT, UUID),
    pf_share(TEXT, UUID, TEXT, TEXT, TEXT, TEXT, INT, TEXT, TEXT), pf_revoke(TEXT, UUID),
    pf_grants_list(TEXT, TEXT, INT), pf_post(TEXT, UUID, JSONB, TEXT),
    pf_read(TEXT, UUID, BIGINT, INT, BIGINT), pf_guest_message(TEXT, UUID, TEXT),
    pf_inbox(TEXT, TIMESTAMPTZ), pf_set_note(TEXT, UUID, TEXT),
    pf_sync(TEXT, JSONB, TIMESTAMPTZ, INT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION orgs_me(TEXT), orgs_members(TEXT), orgs_create_invite(TEXT, INT),
    orgs_redeem_invite(TEXT, TEXT), orgs_revoke_invites(TEXT), orgs_remove_member(TEXT, UUID),
    pf_share(TEXT, UUID, TEXT, TEXT, TEXT, TEXT, INT, TEXT, TEXT), pf_revoke(TEXT, UUID),
    pf_grants_list(TEXT, TEXT, INT), pf_post(TEXT, UUID, JSONB, TEXT),
    pf_read(TEXT, UUID, BIGINT, INT, BIGINT), pf_guest_message(TEXT, UUID, TEXT),
    pf_inbox(TEXT, TIMESTAMPTZ), pf_set_note(TEXT, UUID, TEXT),
    pf_sync(TEXT, JSONB, TIMESTAMPTZ, INT) TO anon, authenticated, service_role;

-- Tell the API to see the new functions now, not on its next schema refresh.
NOTIFY pgrst, 'reload schema';
