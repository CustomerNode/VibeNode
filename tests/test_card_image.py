"""Screenshots in a handoff card given as a file path (2026-10-28).

Sessions wrote ``{"kind": "image", "url": "/tmp/shot.png"}``; the browser asked
the web server for ``/tmp/shot.png`` as a page path and drew a broken image on
every device.  The card now loads such a picture through ``/api/card-image``,
which serves only a real image under home or the temp folder.
"""

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
LAST_CARD = Path(__file__).resolve().parents[1] / "static" / "js" / "last-card.js"


@pytest.fixture
def client():
    from app import create_app
    app = create_app(testing=True)
    with app.test_client() as c:
        yield c


@pytest.fixture
def tmpdir_in_temp():
    d = Path(tempfile.mkdtemp(prefix="vn-card-image-"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


def _get(c, path):
    return c.get("/api/card-image", query_string={"path": str(path)})


def test_serves_a_png_from_the_temp_folder(client, tmpdir_in_temp):
    f = tmpdir_in_temp / "shot.png"
    f.write_bytes(PNG)
    r = _get(client, f)
    assert r.status_code == 200
    assert r.mimetype == "image/png"
    assert r.data == PNG
    assert _get(client, "file://" + str(f)).status_code == 200


def test_judges_by_content_not_name(client, tmpdir_in_temp):
    fake = tmpdir_in_temp / "secret.png"
    fake.write_text("not an image")
    assert _get(client, fake).status_code == 415
    real = tmpdir_in_temp / "shot.txt"
    real.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 16)
    r = _get(client, real)
    assert r.status_code == 200 and r.mimetype == "image/jpeg"


def test_refuses_outside_home_and_temp(client):
    for p in ("/etc/hostname", "/etc/passwd", "/proc/self/environ"):
        assert _get(client, p).status_code == 403


def test_missing_and_empty(client, tmpdir_in_temp):
    assert _get(client, tmpdir_in_temp / "gone.png").status_code == 404
    assert client.get("/api/card-image").status_code == 400


def test_a_relayed_request_is_refused(client, tmpdir_in_temp):
    f = tmpdir_in_temp / "shot.png"
    f.write_bytes(PNG)
    r = client.get("/api/card-image", query_string={"path": str(f)},
                   headers={"X-Forwarded-For": "100.64.0.9"})
    assert r.status_code == 403


SCRIPT = r"""
global.window = global;
global.location = { host: '127.0.0.1:5050' };
global.document = { addEventListener() {}, querySelectorAll() { return []; } };
eval(require('fs').readFileSync(process.argv[1], 'utf8'));
const urls = JSON.parse(process.argv[2]);
const out = urls.map(u => {
  const h = VNLastCard.render({ type: 'done', headline: 'x', links: [{ label: 'S', kind: 'image', url: u }],
                                needs: { kind: 'none', why: 'y' } }, {});
  const m = h.match(/<img class="thumb" src="([^"]*)"/);
  return m ? m[1].replace(/&amp;/g, '&') : null;
});
process.stdout.write(JSON.stringify(out));
"""


def test_the_card_loads_file_paths_through_the_route():
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    urls = ["/tmp/shot.png", "~/shots/a.png", "file:///tmp/b.png", "/api/preview/asset/abc",
            "/static/scratch/x.png", "http://localhost:3000/a.png", "https://example.com/a.png"]
    r = subprocess.run(["node", "-e", SCRIPT, str(LAST_CARD), json.dumps(urls)],
                       capture_output=True, text=True, timeout=30, check=True)
    got = json.loads(r.stdout)
    assert got == [
        "/api/card-image?path=%2Ftmp%2Fshot.png",
        "/api/card-image?path=~%2Fshots%2Fa.png",
        "/api/card-image?path=file%3A%2F%2F%2Ftmp%2Fb.png",
        "/api/preview/asset/abc",
        "/static/scratch/x.png",
        "/api/preview/proxy?u=http%3A%2F%2Flocalhost%3A3000%2Fa.png",
        "https://example.com/a.png",
    ]
