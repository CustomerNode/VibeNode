"""Selenium E2E tests for the Manage dropdown: Duplicate, Fork, Rewind, Fork+Rewind.

Covers:
- Duplicate: immediate full copy, no picker
- Fork: message picker -> new session with messages up to selection
- Rewind Code: message picker -> restore files (with & without snapshots)
- Fork + Rewind: message picker -> fork + restore files
- Icon differentiation: Fork and Fork+Rewind should have distinct icons
"""

import json, time, uuid as uuid_mod
from pathlib import Path
import pytest
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

BASE_URL = "http://127.0.0.1:5050"
CP = Path.home() / ".claude" / "projects"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _uuid():
    return str(uuid_mod.uuid4())

def _ts(m=0, s=0):
    return f"2026-03-10T10:{m:02d}:{s:02d}Z"

def _umsg(c, ts=None, uid=None, sid="t"):
    uid = uid or _uuid()
    return json.dumps({"type":"user","message":{"role":"user","content":c},
                        "timestamp":ts or _ts(),"sessionId":sid,"uuid":uid})

def _amsg(c, ts=None, uid=None, sid="t", tu=None):
    uid = uid or _uuid()
    if tu:
        bl = [{"type":"text","text":c}] if c else []
        for t2 in tu: bl.append({"type":"tool_use",**t2})
        ct = bl
    else: ct = c
    return json.dumps({"type":"assistant","message":{"role":"assistant","content":ct},
                        "timestamp":ts or _ts(),"sessionId":sid,"uuid":uid})

def _snap(mid, tf=None):
    b = {}
    if tf:
        for rp, bn in tf.items():
            b[rp] = {"backupFileName":bn,"version":1,"backupTime":_ts()}
    return json.dumps({"type":"file-history-snapshot","messageId":mid,
        "snapshot":{"messageId":mid,"trackedFileBackups":b,"timestamp":_ts()},
        "isSnapshotUpdate":bool(tf)})

def _ttl(t, sid="t"):
    return json.dumps({"type":"custom-title","customTitle":t,"sessionId":sid})

def _fsd():
    return CP / "C--Users-15512-Documents-ClaudeGUI"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def driver():
    o = webdriver.ChromeOptions()
    o.add_argument("--headless=new"); o.add_argument("--no-sandbox")
    o.add_argument("--disable-gpu"); o.add_argument("--window-size=1400,900")
    d = webdriver.Chrome(options=o); yield d; d.quit()

@pytest.fixture(scope="module")
def sdir(): return _fsd()

@pytest.fixture(scope="module")
def snap_session(sdir):
    """Session with tool_use, file edits, and a snapshot."""
    sid = f"e2e-mgr-snap-{_uuid()[:8]}"
    ua, ub = _uuid(), _uuid()
    lines = [
        _ttl("E2E Manage Snap", sid),
        _umsg("Edit foo", _ts(0,0), ua, sid),
        _amsg("Editing", _ts(0,5), ub, sid,
              tu=[{"name":"Edit","input":{"file_path":"/t/foo.py","old_string":"o","new_string":"n"}}]),
        _snap(ub, {"t/foo.py":"bk_foo"}),
        _umsg("Edit bar", _ts(1,0), _uuid(), sid),
        _amsg("Done bar", _ts(1,5), _uuid(), sid,
              tu=[{"name":"Edit","input":{"file_path":"/t/bar.py","old_string":"o","new_string":"n"}}]),
        _umsg("Final note", _ts(2,0), _uuid(), sid),
        _amsg("All done.", _ts(2,5), _uuid(), sid),
    ]
    p = sdir / f"{sid}.jsonl"
    p.write_text("\n".join(lines)+"\n", encoding="utf-8")
    hd = Path.home()/".claude"/"file-history"/sid
    hd.mkdir(parents=True, exist_ok=True)
    (hd/"bk_foo").write_text("# orig\n", encoding="utf-8")
    yield sid
    p.unlink(missing_ok=True)
    if hd.exists():
        for fi in hd.iterdir(): fi.unlink()
        hd.rmdir()

@pytest.fixture(scope="module")
def plain_session(sdir):
    """Session with no tool_use or snapshots."""
    sid = f"e2e-mgr-plain-{_uuid()[:8]}"
    lines = [
        _ttl("E2E Manage Plain", sid),
        _umsg("Hello there", _ts(0,0), _uuid(), sid),
        _amsg("Hi! How can I help?", _ts(0,5), _uuid(), sid),
        _umsg("What is Python?", _ts(1,0), _uuid(), sid),
        _amsg("Python is a language.", _ts(1,5), _uuid(), sid),
        _umsg("Thanks!", _ts(2,0), _uuid(), sid),
        _amsg("You're welcome!", _ts(2,5), _uuid(), sid),
    ]
    p = sdir / f"{sid}.jsonl"
    p.write_text("\n".join(lines)+"\n", encoding="utf-8")
    yield sid
    p.unlink(missing_ok=True)

def _setup(d, sid, sd):
    d.get(BASE_URL)
    WebDriverWait(d, 10).until(EC.presence_of_element_located((By.TAG_NAME,"header")))
    time.sleep(1)
    pn = sd.name
    d.execute_script("localStorage.setItem(" + repr("activeProject") + "," + repr(pn) + ")")
    time.sleep(0.5)
    d.execute_script("localStorage.setItem('activeSessionId','" + sid + "')")
    d.get(BASE_URL)
    WebDriverWait(d, 10).until(EC.presence_of_element_located((By.TAG_NAME,'header')))
    time.sleep(2)

def _open_picker(d, sid, mode):
    d.execute_script("localStorage.setItem('activeSessionId','" + sid + "');showMessagePicker('" + sid + "','" + mode + "')")
    WebDriverWait(d, 10).until(EC.visibility_of_element_located((By.ID,'pm-overlay')))

def _wait_rows(d, min_count=1, timeout=10):
    WebDriverWait(d, timeout).until(EC.presence_of_element_located((By.CSS_SELECTOR,"#msg-timeline .tl-row")))
    rows = d.find_elements(By.CSS_SELECTOR,"#msg-timeline .tl-row")
    assert len(rows) >= min_count, f"Expected >= {min_count} rows, got {len(rows)}"
    return rows

def _select_row(d, row):
    row.click(); time.sleep(0.3)
    assert "selected" in row.get_attribute("class")
    assert d.find_element(By.ID,"pm-confirm").is_enabled()

def _close_picker(d):
    if d.find_element(By.ID,"pm-overlay").is_displayed():
        d.find_element(By.ID,"pm-cancel").click(); time.sleep(0.5)


# =========================================================================
# 1. DUPLICATE
# =========================================================================

class TestDuplicate:
    def test_load_session(self, driver, sdir, plain_session):
        _setup(driver, plain_session, sdir)

    def test_actions_popup_visible(self, driver):
        driver.execute_script("openActionsPopup()")
        time.sleep(0.3)
        assert driver.find_element(By.ID, "actions-overlay").is_displayed()

    def test_duplicate_button_exists(self, driver):
        btn = driver.find_element(By.ID, "btn-duplicate")
        assert "Duplicate" in btn.text

    def test_duplicate_creates_new_session(self, driver, sdir, plain_session):
        before = len(list(sdir.glob("*.jsonl")))
        driver.find_element(By.ID, "btn-duplicate").click()
        time.sleep(3)
        after = len(list(sdir.glob("*.jsonl")))
        assert after == before + 1

    def test_duplicate_toast(self, driver):
        assert "duplicat" in driver.find_element(By.CSS_SELECTOR, ".toast").text.lower()

    def test_duplicated_file_same_line_count(self, driver, sdir, plain_session):
        orig = sdir / f"{plain_session}.jsonl"
        orig_n = sum(1 for ln in orig.read_text().splitlines() if ln.strip())
        newest = max((f for f in sdir.glob("*.jsonl") if f.stem != plain_session),
                     key=lambda p: p.stat().st_mtime)
        dup_n = sum(1 for ln in newest.read_text().splitlines() if ln.strip())
        assert dup_n == orig_n

    def test_duplicated_file_new_session_id(self, driver, sdir, plain_session):
        newest = max((f for f in sdir.glob("*.jsonl") if f.stem != plain_session),
                     key=lambda p: p.stat().st_mtime)
        for line in newest.read_text().splitlines():
            if not line.strip(): continue
            obj = json.loads(line)
            if "sessionId" in obj:
                assert obj["sessionId"] != plain_session
                break


# =========================================================================
# 2. FORK
# =========================================================================

class TestFork:
    def test_load(self, driver, sdir, plain_session):
        _setup(driver, plain_session, sdir)

    def test_open_picker(self, driver, plain_session):
        _open_picker(driver, plain_session, "fork")
        assert driver.find_element(By.ID,"pm-overlay").is_displayed()

    def test_title_says_fork(self, driver):
        assert "Fork" in driver.find_element(By.CSS_SELECTOR,"#pm-overlay .pm-title").text

    def test_description(self, driver):
        body = driver.find_element(By.CSS_SELECTOR,"#pm-overlay .pm-body").text.lower()
        assert "new session" in body or "forked" in body or "excluded" in body

    def test_timeline_has_rows(self, driver):
        rows = _wait_rows(driver, 2)
        assert len(rows) >= 4

    def test_rows_show_roles(self, driver):
        rows = driver.find_elements(By.CSS_SELECTOR,"#msg-timeline .tl-row")
        roles = [r.find_element(By.CSS_SELECTOR,".tl-role").text.lower() for r in rows]
        assert "me" in roles and "claude" in roles

    def test_rows_show_preview(self, driver):
        row = driver.find_elements(By.CSS_SELECTOR,"#msg-timeline .tl-row")[0]
        assert len(row.find_element(By.CSS_SELECTOR,".tl-preview").text) > 0

    def test_confirm_disabled_initially(self, driver):
        assert not driver.find_element(By.ID,"pm-confirm").is_enabled()

    def test_select_row_enables_confirm(self, driver):
        rows = driver.find_elements(By.CSS_SELECTOR,"#msg-timeline .tl-row")
        _select_row(driver, rows[1])

    def test_only_one_selected(self, driver):
        rows = driver.find_elements(By.CSS_SELECTOR,"#msg-timeline .tl-row")
        rows[0].click(); time.sleep(0.3)
        assert "selected" in rows[0].get_attribute("class")
        assert "selected" not in rows[1].get_attribute("class")

    def test_confirm_fork(self, driver, sdir):
        rows = driver.find_elements(By.CSS_SELECTOR,"#msg-timeline .tl-row")
        _select_row(driver, rows[1])
        before = len(list(sdir.glob("*.jsonl")))
        driver.find_element(By.ID,"pm-confirm").click()
        time.sleep(3)
        assert len(list(sdir.glob("*.jsonl"))) == before + 1

    def test_fork_toast(self, driver):
        assert "fork" in driver.find_element(By.CSS_SELECTOR,".toast").text.lower()

    def test_picker_closed(self, driver):
        time.sleep(0.5)
        assert not driver.find_element(By.ID,"pm-overlay").is_displayed()

    def test_forked_has_fork_title(self, driver, sdir, plain_session):
        newest = max((f for f in sdir.glob("*.jsonl")
                      if f.stem != plain_session and "mgr-plain" not in f.stem),
                     key=lambda p: p.stat().st_mtime)
        assert "[fork]" in newest.read_text()

    def test_forked_has_fewer_lines(self, driver, sdir, plain_session):
        orig_n = sum(1 for ln in (sdir/f"{plain_session}.jsonl").read_text().splitlines() if ln.strip())
        newest = max((f for f in sdir.glob("*.jsonl")
                      if f.stem != plain_session and "mgr-plain" not in f.stem),
                     key=lambda p: p.stat().st_mtime)
        fork_n = sum(1 for ln in newest.read_text().splitlines() if ln.strip())
        assert fork_n < orig_n


class TestForkCancel:
    def test_cancel_no_new_file(self, driver, sdir, plain_session):
        _setup(driver, plain_session, sdir)
        before = len(list(sdir.glob("*.jsonl")))
        _open_picker(driver, plain_session, "fork")
        _wait_rows(driver)
        driver.find_element(By.ID,"pm-cancel").click(); time.sleep(0.5)
        assert not driver.find_element(By.ID,"pm-overlay").is_displayed()
        assert len(list(sdir.glob("*.jsonl"))) == before


class TestForkOverlayDismiss:
    def test_overlay_click_closes(self, driver, plain_session):
        _open_picker(driver, plain_session, "fork")
        _wait_rows(driver)
        overlay = driver.find_element(By.ID,"pm-overlay")
        driver.execute_script(
            "arguments[0].dispatchEvent(new MouseEvent('click',{bubbles:true}))", overlay)
        time.sleep(0.5)
        assert not overlay.is_displayed()


# =========================================================================
# 3. REWIND CODE
# =========================================================================

class TestRewind:
    def test_load(self, driver, sdir, snap_session):
        _setup(driver, snap_session, sdir)

    def test_open_picker(self, driver, snap_session):
        _open_picker(driver, snap_session, "rewind")
        assert driver.find_element(By.ID,"pm-overlay").is_displayed()

    def test_title(self, driver):
        assert "Rewind" in driver.find_element(By.CSS_SELECTOR,"#pm-overlay .pm-title").text

    def test_timeline_rows(self, driver):
        _wait_rows(driver, 2)

    def test_snapshot_icons(self, driver):
        assert len(driver.find_elements(By.CSS_SELECTOR,"#msg-timeline .tl-snap")) >= 1

    def test_confirm_disabled(self, driver):
        assert not driver.find_element(By.ID,"pm-confirm").is_enabled()

    def test_select_snap_row(self, driver):
        rows = driver.find_elements(By.CSS_SELECTOR,"#msg-timeline .tl-row")
        snap_row = next((r for r in rows if r.find_elements(By.CSS_SELECTOR,".tl-snap")), rows[0])
        _select_row(driver, snap_row)

    def test_confirm_rewind(self, driver):
        driver.find_element(By.ID,"pm-confirm").click()
        time.sleep(3)
        t = driver.find_element(By.CSS_SELECTOR,".toast").text.lower()
        assert "restored" in t or "rewind" in t or "file" in t

    def test_picker_closed(self, driver):
        time.sleep(0.5)
        assert not driver.find_element(By.ID,"pm-overlay").is_displayed()


class TestRewindNoSnapshots:
    def test_warning(self, driver, sdir, plain_session):
        _setup(driver, plain_session, sdir)
        _open_picker(driver, plain_session, "rewind")
        WebDriverWait(driver, 10).until(lambda d: d.find_element(By.ID,"msg-timeline").text.strip())
        time.sleep(0.5)
        t = driver.find_element(By.ID,"msg-timeline").text.lower()
        assert "no file snapshots" in t or "not available" in t

    def test_confirm_stays_disabled(self, driver):
        assert not driver.find_element(By.ID,"pm-confirm").is_enabled()

    def test_cancel(self, driver):
        driver.find_element(By.ID,"pm-cancel").click(); time.sleep(0.3)
        assert not driver.find_element(By.ID,"pm-overlay").is_displayed()


class TestRewindCancel:
    def test_open_select_cancel(self, driver, sdir, snap_session):
        _setup(driver, snap_session, sdir)
        _open_picker(driver, snap_session, "rewind")
        rows = _wait_rows(driver)
        _select_row(driver, rows[0])
        driver.find_element(By.ID,"pm-cancel").click(); time.sleep(0.5)
        assert not driver.find_element(By.ID,"pm-overlay").is_displayed()


# =========================================================================
# 4. FORK + REWIND
# =========================================================================

class TestForkRewind:
    def test_load(self, driver, sdir, snap_session):
        _setup(driver, snap_session, sdir)

    def test_open_picker(self, driver, snap_session):
        _open_picker(driver, snap_session, "fork-rewind")
        assert driver.find_element(By.ID,"pm-overlay").is_displayed()

    def test_title(self, driver):
        t = driver.find_element(By.CSS_SELECTOR,"#pm-overlay .pm-title").text
        assert "Fork" in t and "Rewind" in t

    def test_description(self, driver):
        body = driver.find_element(By.CSS_SELECTOR,"#pm-overlay .pm-body").text.lower()
        assert "fork" in body and "restore" in body

    def test_timeline_rows(self, driver):
        _wait_rows(driver, 2)

    def test_snapshot_icons(self, driver):
        assert len(driver.find_elements(By.CSS_SELECTOR,"#msg-timeline .tl-snap")) >= 1

    def test_confirm_disabled(self, driver):
        assert not driver.find_element(By.ID,"pm-confirm").is_enabled()

    def test_select_and_confirm(self, driver, sdir, snap_session):
        rows = driver.find_elements(By.CSS_SELECTOR,"#msg-timeline .tl-row")
        snap_row = next((r for r in rows if r.find_elements(By.CSS_SELECTOR,".tl-snap")), rows[0])
        _select_row(driver, snap_row)
        before = len(list(sdir.glob("*.jsonl")))
        driver.find_element(By.ID,"pm-confirm").click()
        time.sleep(3)
        assert len(list(sdir.glob("*.jsonl"))) == before + 1

    def test_toast(self, driver):
        assert "fork" in driver.find_element(By.CSS_SELECTOR,".toast").text.lower()

    def test_picker_closed(self, driver):
        time.sleep(0.5)
        assert not driver.find_element(By.ID,"pm-overlay").is_displayed()

    def test_title_prefix(self, driver, sdir, snap_session):
        newest = max((f for f in sdir.glob("*.jsonl")
                      if f.stem != snap_session and "mgr-snap" not in f.stem),
                     key=lambda p: p.stat().st_mtime)
        text = newest.read_text()
        assert "[fork+rewind]" in text or "[fork]" in text


class TestForkRewindNoSnapshots:
    def test_warning(self, driver, sdir, plain_session):
        _setup(driver, plain_session, sdir)
        _open_picker(driver, plain_session, "fork-rewind")
        WebDriverWait(driver, 10).until(lambda d: d.find_element(By.ID,"msg-timeline").text.strip())
        time.sleep(0.5)
        t = driver.find_element(By.ID,"msg-timeline").text.lower()
        assert "no file snapshots" in t or "not available" in t

    def test_confirm_stays_disabled(self, driver):
        assert not driver.find_element(By.ID,"pm-confirm").is_enabled()

    def test_cancel(self, driver):
        driver.find_element(By.ID,"pm-cancel").click(); time.sleep(0.3)


class TestForkRewindCancel:
    def test_no_new_file(self, driver, sdir, snap_session):
        _setup(driver, snap_session, sdir)
        _open_picker(driver, snap_session, "fork-rewind")
        rows = _wait_rows(driver)
        _select_row(driver, rows[0])
        before = len(list(sdir.glob("*.jsonl")))
        driver.find_element(By.ID,"pm-cancel").click(); time.sleep(0.5)
        assert not driver.find_element(By.ID,"pm-overlay").is_displayed()
        assert len(list(sdir.glob("*.jsonl"))) == before


# =========================================================================
# 5. ICON DIFFERENTIATION (Fork vs Fork+Rewind)
# =========================================================================

class TestIconDifferentiation:
    def test_load(self, driver, sdir, snap_session):
        _setup(driver, snap_session, sdir)

    def test_fork_and_fork_rewind_icons_differ(self, driver):
        fork_svg = driver.find_element(By.CSS_SELECTOR,"#btn-fork svg").get_attribute("outerHTML")
        fr_svg = driver.find_element(By.CSS_SELECTOR,"#btn-fork-rewind svg").get_attribute("outerHTML")
        assert fork_svg != fr_svg, (
            "Fork and Fork+Rewind buttons should have DIFFERENT icons")

    def test_rewind_icon_distinct(self, driver):
        rw = driver.find_element(By.CSS_SELECTOR,"#btn-rewind svg").get_attribute("outerHTML")
        fk = driver.find_element(By.CSS_SELECTOR,"#btn-fork svg").get_attribute("outerHTML")
        assert rw != fk

    def test_duplicate_icon_distinct(self, driver):
        dup = driver.find_element(By.CSS_SELECTOR,"#btn-duplicate svg").get_attribute("outerHTML")
        fk = driver.find_element(By.CSS_SELECTOR,"#btn-fork svg").get_attribute("outerHTML")
        assert dup != fk


# =========================================================================
# 6. MANAGE DROPDOWN BUTTON STATE
# =========================================================================

class TestManageButtons:
    def test_load(self, driver, sdir, snap_session):
        _setup(driver, snap_session, sdir)

    def test_all_four_present(self, driver):
        driver.execute_script("openActionsPopup()")
        time.sleep(0.3)
        overlay = driver.find_element(By.ID, "actions-overlay")
        ids = [b.get_attribute("id") for b in overlay.find_elements(By.CSS_SELECTOR, ".actions-item")]
        for exp in ("btn-duplicate","btn-fork","btn-rewind","btn-fork-rewind"):
            assert exp in ids, f"{exp} missing"

    def test_labels(self, driver):
        assert "Duplicate" in driver.find_element(By.ID,"btn-duplicate").text
        assert "Fork" in driver.find_element(By.ID,"btn-fork").text
        assert "Rewind" in driver.find_element(By.ID,"btn-rewind").text
        fr = driver.find_element(By.ID,"btn-fork-rewind").text
        assert "Fork" in fr and "Rewind" in fr


# =========================================================================
# 7. API INTEGRATION
# =========================================================================

class TestManageAPIs:
    def test_duplicate_api(self, driver, snap_session):
        import urllib.request
        req = urllib.request.Request(BASE_URL+"/api/duplicate/"+snap_session, method="POST")
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read())
        assert data["ok"] is True and "new_id" in data

    def test_fork_api(self, driver, snap_session):
        import urllib.request
        with urllib.request.urlopen(BASE_URL+"/api/session-timeline/"+snap_session) as resp:
            tl = json.loads(resp.read())
        ln = tl["messages"][1]["line_number"]
        req = urllib.request.Request(BASE_URL+"/api/fork/"+snap_session,
            data=json.dumps({"up_to_line":ln}).encode(),
            headers={"Content-Type":"application/json"}, method="POST")
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read())
        assert data["ok"] is True and "[fork]" in data["title"]

    def test_rewind_api(self, driver, snap_session):
        import urllib.request
        with urllib.request.urlopen(BASE_URL+"/api/session-timeline/"+snap_session) as resp:
            tl = json.loads(resp.read())
        ln = max(m["line_number"] for m in tl["messages"])
        req = urllib.request.Request(BASE_URL+"/api/rewind/"+snap_session,
            data=json.dumps({"up_to_line":ln}).encode(),
            headers={"Content-Type":"application/json"}, method="POST")
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read())
        assert data["ok"] is True
        assert isinstance(data.get("files_restored"), list)

    def test_fork_rewind_api(self, driver, snap_session):
        import urllib.request
        with urllib.request.urlopen(BASE_URL+"/api/session-timeline/"+snap_session) as resp:
            tl = json.loads(resp.read())
        ln = max(m["line_number"] for m in tl["messages"])
        req = urllib.request.Request(BASE_URL+"/api/fork-rewind/"+snap_session,
            data=json.dumps({"up_to_line":ln}).encode(),
            headers={"Content-Type":"application/json"}, method="POST")
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read())
        assert data["ok"] is True and "new_id" in data

    def test_duplicate_404(self, driver):
        import urllib.request
        req = urllib.request.Request(BASE_URL+"/api/duplicate/does-not-exist", method="POST")
        try: urllib.request.urlopen(req); assert False
        except urllib.error.HTTPError as e: assert e.code == 404

    def test_fork_missing_line_400(self, driver, snap_session):
        import urllib.request
        req = urllib.request.Request(BASE_URL+"/api/fork/"+snap_session,
            data=json.dumps({}).encode(),
            headers={"Content-Type":"application/json"}, method="POST")
        try: urllib.request.urlopen(req); assert False
        except urllib.error.HTTPError as e: assert e.code == 400


# =========================================================================
# 8. TIMELINE CONTENT
# =========================================================================

class TestTimelineContent:
    def test_load(self, driver, sdir, snap_session):
        _setup(driver, snap_session, sdir)

    def test_indices(self, driver, snap_session):
        _open_picker(driver, snap_session, "fork")
        rows = _wait_rows(driver, 2)
        assert "#" in rows[0].find_element(By.CSS_SELECTOR,".tl-idx").text
        _close_picker(driver)

    def test_timestamps(self, driver, snap_session):
        _open_picker(driver, snap_session, "fork")
        rows = _wait_rows(driver, 2)
        assert len(rows[0].find_element(By.CSS_SELECTOR,".tl-ts").text) > 0
        _close_picker(driver)

    def test_file_changes(self, driver, snap_session):
        _open_picker(driver, snap_session, "fork")
        rows = _wait_rows(driver, 2)
        found = any(r.find_elements(By.CSS_SELECTOR,".tl-files") for r in rows)
        assert found, "Expected file change badges"
        _close_picker(driver)

    def test_fork_no_snap_warning(self, driver, sdir, plain_session):
        """Fork mode should NOT warn about missing snapshots."""
        _setup(driver, plain_session, sdir)
        _open_picker(driver, plain_session, "fork")
        _wait_rows(driver, 2)
        t = driver.find_element(By.ID,"msg-timeline").text.lower()
        assert "no file snapshots" not in t
        _close_picker(driver)
