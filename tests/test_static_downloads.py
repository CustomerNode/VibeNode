"""A file a summary card offers as a Download is saved, not shown.

iPhone Safari did nothing with a .zip the server sent ``inline`` as Windows'
registry type ``application/x-zip-compressed``: the card's Download button
was dead on the phone.  Download-type files under /static now go out as an
attachment with the standard type; pages, scripts and pictures are untouched.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from app import create_app


@pytest.fixture
def client():
    app = create_app(testing=True)
    return app.test_client(), Path(app.static_folder)


@pytest.fixture
def scratch_file(client):
    _c, static = client
    made = []

    def make(ext: str, body: bytes = b"PK\x03\x04 fictional") -> str:
        d = static / "scratch"
        d.mkdir(exist_ok=True)
        name = f"test-download-{uuid.uuid4().hex[:8]}{ext}"
        (d / name).write_bytes(body)
        made.append(d / name)
        return f"/static/scratch/{name}"

    yield make
    for p in made:
        p.unlink(missing_ok=True)


def test_a_zip_is_sent_as_an_attachment_with_the_standard_type(client, scratch_file):
    c, _ = client
    url = scratch_file(".zip")
    r = c.get(url)
    assert r.status_code == 200
    assert r.headers["Content-Type"] == "application/zip"
    assert r.headers["Content-Disposition"] == f'attachment; filename="{url.rsplit("/", 1)[1]}"'


@pytest.mark.parametrize("ext", [".pptx", ".docx", ".xlsx"])
def test_office_files_are_attachments_too(client, scratch_file, ext):
    c, _ = client
    r = c.get(scratch_file(ext))
    assert r.headers["Content-Disposition"].startswith("attachment;")
    assert "x-zip" not in r.headers["Content-Type"]


@pytest.mark.parametrize("ext", [".html", ".png", ".js"])
def test_pages_pictures_and_scripts_are_left_alone(client, scratch_file, ext):
    c, _ = client
    r = c.get(scratch_file(ext, b"<p>x</p>"))
    assert "attachment" not in r.headers.get("Content-Disposition", "")


def test_a_missing_file_is_not_dressed_up_as_a_download(client):
    c, _ = client
    r = c.get("/static/scratch/nothing-here-at-all.zip")
    assert r.status_code == 404 and "attachment" not in r.headers.get("Content-Disposition", "")
