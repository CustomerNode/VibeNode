"""Selenium E2E tests for Rewind Code."""
import json, time, uuid as uuid_mod
from pathlib import Path
import pytest
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
BASE_URL = "http://127.0.0.1:5050"
CP = Path.home() / ".claude" / "projects"
def _uuid(): return str(uuid_mod.uuid4())
def _ts(m=0, s=0): return f"2026-03-10T10:{m:02d}:{s:02d}Z"
def _umsg(c, ts=None, uid=None, sid="t"):
    uid = uid or _uuid()
    return json.dumps({"type":"user","message":{"role":"user","content":c},"timestamp":ts or _ts(),"sessionId":sid,"uuid":uid})
def _amsg(c, ts=None, uid=None, sid="t", tu=None):
    uid = uid or _uuid()
    if tu:
        bl = [{"type":"text","text":c}] if c else []
        for t2 in tu: bl.append({"type":"tool_use",**t2})
        ct = bl
    else: ct = c
    return json.dumps({"type":"assistant","message":{"role":"assistant","content":ct},"timestamp":ts or _ts(),"sessionId":sid,"uuid":uid})
def _snap(mid, tf=None):
    b = {}
    if tf:
        for rp, bn in tf.items(): b[rp] = {"backupFileName":bn,"version":1,"backupTime":_ts()}
    return json.dumps({"type":"file-history-snapshot","messageId":mid,"snapshot":{"messageId":mid,"trackedFileBackups":b,"timestamp":_ts()},"isSnapshotUpdate":bool(tf)})
def _ttl(t, sid="t"): return json.dumps({"type":"custom-title","customTitle":t,"sessionId":sid})
def _fsd():
    return CP / "C--Users-15512-Documents-ClaudeGUI"
@pytest.fixture(scope="module")
def driver():
    o = webdriver.ChromeOptions()
    o.add_argument("--headless=new"); o.add_argument("--no-sandbox")
    o.add_argument("--disable-gpu"); o.add_argument("--window-size=1400,900")
    d = webdriver.Chrome(options=o); yield d; d.quit()
@pytest.fixture(scope="module")
def sdir(): return _fsd()
@pytest.fixture(scope="module")
def ss(sdir):
    sid = f"e2e-rw-{_uuid()[:8]}"; ua, ub = _uuid(), _uuid()
    lines = [_ttl("E2E Rewind", sid), _umsg("Edit foo", _ts(0,0), ua, sid),
        _amsg("Editing", _ts(0,5), ub, sid, tu=[{"name":"Edit","input":{"file_path":"/t/foo.py","old_string":"o","new_string":"n"}}]),
        _snap(ub, {"t/foo.py":"bk1"}), _umsg("Edit bar", _ts(1,0), _uuid(), sid),
        _amsg("Done bar", _ts(1,5), _uuid(), sid, tu=[{"name":"Edit","input":{"file_path":"/t/bar.py","old_string":"o","new_string":"n"}}])]
    p = sdir / f"{sid}.jsonl"; p.write_text(chr(10).join(lines)+chr(10), encoding="utf-8")
    hd = Path.home()/".claude"/"file-history"/sid; hd.mkdir(parents=True, exist_ok=True)
    (hd/"bk1").write_text("# orig"+chr(10), encoding="utf-8")
    yield sid
    p.unlink(missing_ok=True)
    if hd.exists():
        for fi in hd.iterdir(): fi.unlink()
        hd.rmdir()
@pytest.fixture(scope="module")
def sns(sdir):
    sid = f"e2e-ns-{_uuid()[:8]}"
    lines = [_ttl("No Snap", sid), _umsg("Hi", _ts(0,0), _uuid(), sid),
        _amsg("Hey!", _ts(0,5), _uuid(), sid), _umsg("Ok?", _ts(1,0), _uuid(), sid),
        _amsg("Yes.", _ts(1,5), _uuid(), sid)]
    p = sdir / f"{sid}.jsonl"; p.write_text(chr(10).join(lines)+chr(10), encoding="utf-8")
    yield sid; p.unlink(missing_ok=True)
def _su(d, sid, sd):
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
def _op(d, sid, mode):
    d.execute_script("localStorage.setItem('activeSessionId','" + sid + "');showMessagePicker('" + sid + "','" + mode + "')")
    WebDriverWait(d, 10).until(EC.visibility_of_element_located((By.ID,'pm-overlay')))
class TestRewindSnap:
    def test_load(self, driver, sdir, ss):
        _su(driver, ss, sdir)
    def test_open(self, driver, ss):
        _op(driver, ss, "rewind")
        assert driver.find_element(By.ID,"pm-overlay").is_displayed()
    def test_title(self, driver):
        assert "Rewind" in driver.find_element(By.CSS_SELECTOR,"#pm-overlay .pm-title").text
    def test_rows(self, driver):
        WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.CSS_SELECTOR,"#msg-timeline .tl-row")))
        assert len(driver.find_elements(By.CSS_SELECTOR,"#msg-timeline .tl-row")) >= 2
    def test_snap_icons(self, driver):
        assert len(driver.find_elements(By.CSS_SELECTOR,"#msg-timeline .tl-snap")) >= 1
    def test_btn_off(self, driver):
        assert not driver.find_element(By.ID,"pm-confirm").is_enabled()
    def test_select(self, driver):
        rows = driver.find_elements(By.CSS_SELECTOR,"#msg-timeline .tl-row")
        t = next((r for r in rows if r.find_elements(By.CSS_SELECTOR,".tl-snap")), rows[0])
        t.click(); time.sleep(0.3)
        assert "selected" in t.get_attribute("class")
        assert driver.find_element(By.ID,"pm-confirm").is_enabled()
    def test_confirm(self, driver):
        driver.find_element(By.ID,"pm-confirm").click()
        WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.CSS_SELECTOR,".toast")))
        t = driver.find_element(By.CSS_SELECTOR,".toast").text.lower()
        assert "restored" in t or "rewind" in t or "file" in t
    def test_closed(self, driver):
        time.sleep(0.5)
        assert not driver.find_element(By.ID,"pm-overlay").is_displayed()
class TestCancel:
    def test_cancel(self, driver, ss):
        _op(driver, ss, "rewind")
        WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.CSS_SELECTOR,"#msg-timeline .tl-row")))
        driver.find_element(By.ID,"pm-cancel").click()
        time.sleep(0.5)
        assert not driver.find_element(By.ID,"pm-overlay").is_displayed()
class TestNoSnap:
    def test_warn(self, driver, sns):
        _op(driver, sns, "rewind")
        WebDriverWait(driver, 10).until(lambda d: d.find_element(By.ID,"msg-timeline").text.strip())
        time.sleep(0.5)
        t = driver.find_element(By.ID,"msg-timeline").text.lower()
        assert "no file snapshots" in t or "not available" in t
        assert not driver.find_element(By.ID,"pm-confirm").is_enabled()
        driver.find_element(By.ID,"pm-cancel").click()
        time.sleep(0.3)
class TestFork:
    def test_loads(self, driver, sns):
        _op(driver, sns, "fork")
        WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.CSS_SELECTOR,"#msg-timeline .tl-row")))
        assert len(driver.find_elements(By.CSS_SELECTOR,"#msg-timeline .tl-row")) >= 2
        driver.find_element(By.ID,"pm-cancel").click(); time.sleep(0.3)
    def test_title(self, driver, sns):
        _op(driver, sns, "fork")
        assert "Fork" in driver.find_element(By.CSS_SELECTOR,"#pm-overlay .pm-title").text
        driver.find_element(By.ID,"pm-cancel").click(); time.sleep(0.3)
class TestForkRewind:
    def test_title(self, driver, ss):
        _op(driver, ss, "fork-rewind")
        t = driver.find_element(By.CSS_SELECTOR,"#pm-overlay .pm-title").text
        assert "Fork" in t and "Rewind" in t
        driver.find_element(By.ID,"pm-cancel").click(); time.sleep(0.3)
    def test_snaps(self, driver, ss):
        _op(driver, ss, "fork-rewind")
        WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.CSS_SELECTOR,"#msg-timeline .tl-row")))
        assert len(driver.find_elements(By.CSS_SELECTOR,"#msg-timeline .tl-snap")) >= 1
        driver.find_element(By.ID,"pm-cancel").click(); time.sleep(0.3)
class TestDropdown:
    def test_btn(self, driver, sdir, ss):
        _su(driver, ss, sdir)
        assert driver.find_element(By.ID,"btn-rewind") is not None
class TestAPI:
    def test_timeline(self, driver, ss):
        import urllib.request
        url = BASE_URL + "/api/session-timeline/" + ss
        with urllib.request.urlopen(url) as resp:
            r = json.loads(resp.read())
        assert r.get("has_snapshots") is True
        assert len(r.get("messages", [])) >= 2
        assert any(m.get("has_snapshot") for m in r["messages"])
    def test_restore(self, driver, ss):
        import urllib.request
        url = BASE_URL + "/api/session-timeline/" + ss
        with urllib.request.urlopen(url) as resp:
            tl = json.loads(resp.read())
        ln = next(m["line_number"] for m in tl["messages"] if m.get("has_snapshot"))
        req = urllib.request.Request(BASE_URL + "/api/rewind/" + ss,
            data=json.dumps({"up_to_line": ln}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req) as resp:
            r = json.loads(resp.read())
        assert r.get("ok") is True
        assert isinstance(r.get("files_restored"), list)
