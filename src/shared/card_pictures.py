"""Keep a copy of every picture an end-of-turn summary shows.

A summary links its screenshots by address (``/static/scratch/x.png``, a
rendered preview, a file path).  The stop checks them once, when the turn
ends; sessions later delete, move or rename those files, and the old summary
would draw a broken picture.

So when a summary passes, every picture it links that is a file on this
machine is copied here, content-addressed, and remembered under its address
and the summary it came from.  ``GET /api/card-picture`` serves the copy, and
the card asks for it when the picture at the original address fails to load.

Layout (``data/card_pictures/``, gitignored):
  ``<sha256>.<ext>``            the picture, once per content
  ``index/<sha256(address)>.jsonl``  one line per summary that showed it:
                                {"sha", "ext", "key", "at"}; append-only, so
                                the engine, the check and the settings-file
                                copy can write at once.

``key`` is ``card_key(title, headline)``: the same address shown by two
summaries (a file overwritten in between) finds the copy its own summary saw.

``VIBENODE_CARD_PICTURES=0`` turns saving off (the test suite does);
``VIBENODE_CARD_PICTURES_DIR`` moves the store.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import time
import urllib.parse
from pathlib import Path
from typing import Iterator, Optional

REPO = Path(__file__).resolve().parents[2]
MAX_BYTES = 20 * 1024 * 1024
_OWN_HOST = re.compile(r"^https?://(?:localhost|127\.0\.0\.1|\[::1\])(?::5050)?(?=/)", re.I)
_PIC_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp")


def store_dir() -> Path:
    d = os.environ.get("VIBENODE_CARD_PICTURES_DIR")
    return Path(d) if d else REPO / "data" / "card_pictures"


def enabled() -> bool:
    return os.environ.get("VIBENODE_CARD_PICTURES") != "0"


def card_key(title, headline) -> str:
    s = (str(title or "").strip() + "\n" + str(headline or "").strip()).encode("utf-8")
    return hashlib.sha256(s).hexdigest()[:16]


def normalize(url) -> str:
    """One spelling per address: VibeNode's own host dropped, ``file://``
    dropped, the query and fragment of an app path dropped."""
    u = str(url or "").strip()
    u = _OWN_HOST.sub("", u)
    if u.lower().startswith("file://"):
        u = urllib.parse.unquote(u[7:])
    if u.startswith("/api/card-image?"):
        q = urllib.parse.parse_qs(u.split("?", 1)[1]).get("path") or [""]
        return normalize(q[0])
    if u.startswith(("/static/", "/api/preview/")):
        u = urllib.parse.unquote(re.sub(r"[?#].*$", "", u))
    return u


def _image_type(head: bytes) -> Optional[str]:
    if head.startswith(b"\x89PNG"):
        return "png"
    if head[:3] == b"\xff\xd8\xff":
        return "jpg"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    return None


MIME = {"png": "image/png", "jpg": "image/jpeg", "gif": "image/gif", "webp": "image/webp"}


def local_file(url) -> Optional[Path]:
    """The file on this machine an address names, or None."""
    u = normalize(url)
    if not u:
        return None
    m = re.match(r"^/api/preview/(?:asset|thumb)/([0-9a-f]{32})$", u)
    if m:
        return REPO / "data" / "previews" / (m.group(1) + ".png")
    if u.startswith("/static/"):
        root = (REPO / "src" / "web" / "static").resolve()
        try:
            p = (root / u[len("/static/"):]).resolve()
            p.relative_to(root)
        except (OSError, ValueError):
            return None
        return p
    if u.startswith("~/") or re.match(r"^[A-Za-z]:[\\/]", u) or (u.startswith("/") and not u.startswith("//")):
        try:
            p = Path(u).expanduser().resolve()
        except (OSError, RuntimeError):
            return None
        roots = [Path.home().resolve(), Path(tempfile.gettempdir()).resolve()]
        if any(p == r or r in p.parents for r in roots):
            return p
    return None


def _strings(o) -> Iterator[str]:
    if isinstance(o, str):
        yield o
    elif isinstance(o, dict):
        for v in o.values():
            yield from _strings(v)
    elif isinstance(o, list):
        for v in o:
            yield from _strings(v)


def picture_urls(card) -> list:
    """Every picture address a summary shows: image links, look entries, and
    each Decide screen's url (when a picture) and ``image``."""
    if not isinstance(card, dict):
        return []
    out = []
    needs = card.get("needs") if isinstance(card.get("needs"), dict) else {}
    for l in (card.get("links") or []) + (needs.get("look") or []):
        if isinstance(l, dict) and isinstance(l.get("url"), str):
            if l.get("kind") == "image" or re.sub(r"[?#].*$", "", l["url"]).lower().endswith(_PIC_EXT):
                out.append(l["url"])
    for r in needs.get("replies") or []:
        if not isinstance(r, dict):
            continue
        for s in r.get("screens") or []:
            if not isinstance(s, dict):
                continue
            u = s.get("url")
            if isinstance(u, str) and re.sub(r"[?#].*$", "", u).lower().endswith(_PIC_EXT):
                out.append(u)
            out.extend(_strings(s.get("image")))
    return list(dict.fromkeys(u for u in out if u))


def _index_file(url) -> Path:
    return store_dir() / "index" / (hashlib.sha256(normalize(url).encode("utf-8")).hexdigest()[:32] + ".jsonl")


def save_picture(url, key: str) -> Optional[str]:
    """Copy the file *url* names into the store under *key*; its sha or None."""
    p = local_file(url)
    if p is None or not p.is_file():
        return None
    try:
        if p.stat().st_size > MAX_BYTES:
            return None
        data = p.read_bytes()
    except OSError:
        return None
    ext = _image_type(data[:16])
    if not ext:
        return None
    sha = hashlib.sha256(data).hexdigest()
    d = store_dir()
    dst = d / (sha + "." + ext)
    (d / "index").mkdir(parents=True, exist_ok=True)
    if not dst.is_file():
        tmp = d / (".%s.%d.tmp" % (sha, os.getpid()))
        tmp.write_bytes(data)
        os.replace(tmp, dst)
    idx = _index_file(url)
    line = json.dumps({"sha": sha, "ext": ext, "key": key, "at": int(time.time())})
    try:
        for old in idx.read_text(encoding="utf-8").splitlines():
            o = json.loads(old)
            if o.get("sha") == sha and o.get("key") == key:
                return sha          # already remembered for this summary
    except (OSError, ValueError):
        pass
    with open(idx, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    return sha


def save_card(card) -> int:
    """Copy every picture *card* shows; how many were saved.  Never raises."""
    if not enabled() or not isinstance(card, dict):
        return 0
    n = 0
    key = card_key(card.get("title"), card.get("headline"))
    for u in picture_urls(card):
        try:
            if save_picture(u, key):
                n += 1
        except Exception:  # noqa: BLE001 — a picture never blocks a summary
            pass
    return n


def find(url, key: str = "") -> Optional[Path]:
    """The saved copy for *url*: the one its own summary saw, else the newest."""
    try:
        lines = _index_file(url).read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    rows = []
    for ln in lines:
        try:
            o = json.loads(ln)
        except ValueError:
            continue
        if isinstance(o, dict) and re.fullmatch(r"[0-9a-f]{64}", str(o.get("sha"))) and o.get("ext") in MIME:
            rows.append(o)
    mine = [o for o in rows if key and o.get("key") == key]
    for o in reversed(mine or rows):
        p = store_dir() / (o["sha"] + "." + o["ext"])
        if p.is_file():
            return p
    return None


def backfill(projects_dir: Optional[Path] = None) -> tuple:
    """Save the pictures of every summary in every transcript that are still
    on disk.  (cards, pictures saved)."""
    from shared.handoff_status import _BLOCK  # noqa: WPS433
    base = projects_dir or Path.home() / ".claude" / "projects"
    cards = saved = 0
    for f in base.glob("*/*.jsonl"):
        try:
            fh = open(f, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for ln in fh:
                if "```handoff" not in ln or '"assistant"' not in ln:
                    continue
                try:
                    e = json.loads(ln)
                except ValueError:
                    continue
                if e.get("type") != "assistant":
                    continue
                for c in (e.get("message") or {}).get("content") or []:
                    if not isinstance(c, dict) or c.get("type") != "text":
                        continue
                    for m in _BLOCK.finditer(c.get("text") or ""):
                        try:
                            card = json.loads(m.group(2))
                        except ValueError:
                            continue
                        cards += 1
                        saved += save_card(card)
    return cards, saved


if __name__ == "__main__":
    import sys
    if "--backfill" in sys.argv:
        print("cards %d, pictures saved %d" % backfill())
    else:
        print(__doc__)
