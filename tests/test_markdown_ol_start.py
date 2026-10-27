"""A numbered list keeps the author's numbering after a heading.

A reply listing "1-5", a bold heading, then "6-9" rendered the second group
as 1-4: ``mdParse`` emitted a bare ``<ol>`` for every block and dropped the
first number, so on a phone the list read "there is no 6" (2026-10-27).
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

MARKDOWN = Path(__file__).resolve().parents[1] / "static" / "js" / "markdown.js"

SCRIPT = """
global.window = global; global.document = { createElement: () => ({}) };
eval(require('fs').readFileSync(process.argv[1], 'utf8'));
const md = "**A**\\n\\n1. one\\n2. two\\n\\n**B**\\n\\n6. six\\n7. seven\\n\\n**C**\\n\\n10. ten\\n";
process.stdout.write(JSON.stringify(mdParse(md).match(/<ol[^>]*>/g)));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_ordered_list_resumes_at_its_own_number():
    out = subprocess.run(["node", "-e", SCRIPT, str(MARKDOWN)],
                         capture_output=True, text=True, timeout=30, check=True)
    assert json.loads(out.stdout) == ["<ol>", '<ol start="6">', '<ol start="10">']
