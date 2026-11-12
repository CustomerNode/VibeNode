"""VibeNode Orgs: people connected through one cloud storage project.

An org lives in a Supabase project (the creator's cloud storage). Each member's
VibeNode holds only the project's publishable key and the member's own secret
token, and acts through the database functions in
``src/web/db/migrations/007_vibenode_orgs.sql``, which check every call.

* ``store``  this VibeNode's membership (``data/vibenode_orgs.json``) and the calls.
* ``invite`` the copyable, single-use join prompt.
* ``phone``  Phone a Friend: open one session to one member, post its thread,
  pick up their messages, ping on a new call.
"""
