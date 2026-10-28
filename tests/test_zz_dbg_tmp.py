import json
from tests.test_code_pipeline import *  # noqa
from tests.test_code_pipeline import make_valid_run, run_criterion, evidence_sha
def test_dbg(repo):
    run_dir, sess = make_valid_run(repo)
    p = run_dir / "evidence" / "lint.json"
    import pipeline.evidence as ev
    p = run_dir / ev.EVIDENCE_DIR / "lint.json"
    before = evidence_sha(p); b = json.loads(p.read_text())
    lint, lint_path = run_criterion(run_dir, "lint")
    a = json.loads(lint_path.read_text())
    print("SAME", before == evidence_sha(lint_path), b["started"], a["started"], b["elapsed_s"], a["elapsed_s"])
