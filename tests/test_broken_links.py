"""A summary's pictures and pages on this app must load (pictures linked as
/api/preview/asset/<upload name> can open to nothing on a phone).  Smart termination checks this app's own addresses against
the files that serve them and sends back any that are missing."""
import json
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from engine import handoff_rules as hr  # noqa: E402


@pytest.fixture(autouse=True)
def _check_on(monkeypatch):
    monkeypatch.setenv("VIBENODE_LINK_CHECK", "1")
    # A stopped port is retried for _PORT_GRACE_S; keep the dead-port tests
    # quick.  The comeback test below sets its own.
    monkeypatch.setattr(hr, "_PORT_GRACE_S", 0.3)


@pytest.fixture
def real_files(tmp_path, monkeypatch):
    static, previews = tmp_path / "static", tmp_path / "previews"
    (static / "scratch").mkdir(parents=True)
    previews.mkdir()
    (static / "scratch" / "today.png").write_bytes(b"png")
    (static / "scratch" / "page.html").write_text("<p>x</p>", encoding="utf-8")
    pid = uuid.uuid4().hex
    (previews / (pid + ".png")).write_bytes(b"png")
    monkeypatch.setattr(hr, "_STATIC_DIR", static)
    monkeypatch.setattr(hr, "_PREVIEW_DIR", previews)
    return pid


def _card(**extra):
    c = {"type": "explain", "title": "Picture links", "headline": "x", "answer": "y"}
    c.update(extra)
    return c


def test_the_owners_case_is_sent_back(real_files):
    card = _card(links=[{"label": "Today", "kind": "image", "url": "/api/preview/asset/20261030-111644-image.png"}])
    probs = hr.broken_link_problems(card)
    assert probs and "20261030-111644-image.png" in probs[0] and "src/web/static/scratch" in probs[0]
    assert "open to nothing" in (hr.review("```handoff\n" + json.dumps(card) + "\n```") or "")


def test_real_pictures_and_pages_pass(real_files):
    card = _card(links=[{"label": "a", "kind": "image", "url": "/static/scratch/today.png"},
                        {"label": "b", "kind": "image", "url": "/api/preview/asset/" + real_files}],
                 validate={"how": "open", "url": "/static/scratch/page.html?theme=dark#a"},
                 needs={"kind": "none", "look": [{"label": "p", "kind": "link",
                                                  "url": "http://127.0.0.1:5050/static/scratch/page.html"}]})
    assert hr.broken_link_problems(card) == []


@pytest.mark.parametrize("url", ["/static/scratch/missing.png", "/static/scratch/missing.html#a",
                                 "/api/preview/asset/" + "0" * 32, "/static/../daemon/handoff_rules.py",
                                 "http://localhost:5050/static/scratch/gone.png"])
def test_missing_or_escaping_addresses_are_sent_back(real_files, url):
    assert hr.broken_link_problems(_card(links=[{"label": "x", "kind": "image", "url": url}]))


def test_screens_and_files_are_checked_too(real_files):
    card = _card(needs={"kind": "decision", "replies": [{"key": "A", "label": "a", "reply": "a",
                                                         "screens": [{"label": "s", "url": "/static/scratch/nope.html#a"}]}]},
                 deliverable={"files": [{"label": "zip", "url": "/static/scratch/nope.zip"}]})
    p = hr.broken_link_problems(card)[0]
    assert "screens[0]" in p and "files[0]" in p


def test_other_sites_and_other_routes_are_not_checked(real_files, running_port):
    card = _card(links=[{"label": "x", "kind": "link", "url": "https://example.com/a.png"},
                        {"label": "y", "kind": "link", "url": "/?project=x"},
                        {"label": "w", "kind": "link", "url": "/settings"},
                        {"label": "z", "kind": "link", "url": "http://127.0.0.1:%d/" % running_port}])
    assert hr.broken_link_problems(card) == []


@pytest.fixture
def running_port():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(4)
    yield s.getsockname()[1]
    s.close()


def _closed_port():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# Pictures given as file paths (a temp-folder screenshot that is gone) and
# pages whose demo server has stopped are checked too, not only this app's own
# addresses.
@pytest.mark.parametrize("url", ["{tmp}/gone.png", "file://{tmp}/gone.png", "~/vn-no-such-picture-{uid}.png"])
def test_a_picture_given_as_a_missing_file_is_sent_back(real_files, tmp_path, url):
    u = url.format(tmp=tmp_path, uid=uuid.uuid4().hex)
    assert hr.broken_link_problems(_card(links=[{"label": "x", "kind": "image", "url": u}]))


def test_a_picture_or_folder_on_disk_passes(real_files, tmp_path):
    (tmp_path / "shot.png").write_bytes(b"png")
    card = _card(links=[{"label": "x", "kind": "image", "url": str(tmp_path / "shot.png")},
                        {"label": "d", "kind": "file", "url": str(tmp_path)}])
    assert hr.broken_link_problems(card) == []


def test_a_decide_screens_picture_is_checked(real_files, tmp_path):
    card = _card(needs={"kind": "decision", "replies": [{"key": "A", "label": "a", "reply": "a", "screens": [
        {"label": "s", "url": "/static/scratch/page.html#a",
         "image": {"web": {"light": "/static/scratch/today.png", "dark": str(tmp_path / "gone.png")}}}]}]})
    p = hr.broken_link_problems(card)
    assert p and "image.web.dark" in p[0] and "image.web.light" not in p[0]


def test_a_page_on_a_stopped_server_is_sent_back(real_files, running_port):
    dead = _closed_port()
    proxied = "https://box.example.ts.net/api/preview/proxy?u=http%3A%2F%2Flocalhost%3A" + str(dead) + "%2F"
    card = _card(validate={"how": "open", "url": "http://127.0.0.1:%d/app" % dead},
                 needs={"kind": "none", "look": [{"label": "p", "kind": "link", "url": proxied},
                                                 {"label": "q", "kind": "link", "url": "http://localhost:%d/" % running_port}]})
    p = " ".join(hr.broken_link_problems(card))
    assert "not running" in p and "validate.url" in p and "needs.look[0]" in p and "needs.look[1]" not in p


def test_a_dev_server_restarting_is_not_called_stopped(real_files, monkeypatch):
    """Django's runserver and Vite restart themselves when a session edits
    code and refuse connections for a moment.  A card checked in that moment
    was sent back saying the server was not running; the port is now retried
    and passes once it answers again."""
    import socket
    import threading
    monkeypatch.setattr(hr, "_PORT_GRACE_S", 5.0)
    port = _closed_port()
    up = threading.Event()
    srv = socket.socket()

    def come_back():
        up.wait(0.6)
        srv.bind(("127.0.0.1", port))
        srv.listen(4)

    t = threading.Thread(target=come_back, daemon=True)
    t.start()
    try:
        card = _card(validate={"how": "open", "url": "http://127.0.0.1:%d/app" % port})
        assert hr.broken_link_problems(card) == []
    finally:
        t.join(2)
        srv.close()


def test_the_viewer_and_the_proxy_wait_for_a_restarting_server():
    """The full-screen viewer and the phone's proxy page used to say "This
    page's server has stopped" after one failed try and never look again;
    both now wait, retry and load the page when it answers."""
    js = (ROOT / "src" / "web" / "static" / "js" / "last-card.js").read_text(encoding="utf-8")
    body = js[js.index("function _noteStoppedServer"):]
    body = body[:body.index("\n  }\n") + 4]
    assert "setInterval" in body and "frame.replaceWith" not in body and "Try again" in body
    assert "server has stopped" not in js
    py = (ROOT / "src" / "web" / "routes" / "preview_api.py").read_text(encoding="utf-8")
    assert "X-VN-Proxy-Down" in py and "setInterval" in py and "server has stopped" not in py


def test_the_viewer_asks_the_proxy_whether_a_server_answers():
    """A direct no-cors fetch to another port is rejected by the browser when
    that server sends Cross-Origin-Resource-Policy: same-origin, exactly as
    when nothing listens, so a running server showed "Waiting" forever.  The
    viewer asks the preview proxy instead and reads its down mark."""
    js = (ROOT / "src" / "web" / "static" / "js" / "last-card.js").read_text(encoding="utf-8")
    body = js[js.index("function _noteStoppedServer"):]
    body = body[:body.index("\n  }\n") + 4]
    assert "no-cors" not in body
    assert "/api/preview/proxy?u=" in body and "X-VN-Proxy-Down" in body and "'HEAD'" in body


def test_the_proxy_answers_a_head_without_the_down_mark_when_the_server_is_up():
    import socket
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from web import create_app

    class H(BaseHTTPRequestHandler):
        def do_HEAD(self):
            self.send_response(200)
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    s = socket.socket(); s.bind(("127.0.0.1", 0)); dead = s.getsockname()[1]; s.close()
    try:
        c = create_app(testing=True).test_client()
        up = c.head("/api/preview/proxy?u=http://127.0.0.1:%d/" % srv.server_port)
        assert up.status_code == 200 and not up.headers.get("X-VN-Proxy-Down")
        down = c.head("/api/preview/proxy?u=http://127.0.0.1:%d/" % dead)
        assert down.headers.get("X-VN-Proxy-Down") == "1"
    finally:
        srv.shutdown()


def test_it_can_be_turned_off(real_files, monkeypatch):
    monkeypatch.setenv("VIBENODE_LINK_CHECK", "0")
    card = _card(links=[{"label": "x", "kind": "image", "url": "/static/scratch/missing.png"}])
    assert hr.broken_link_problems(card) == []
