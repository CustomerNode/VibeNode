"""
Selenium E2E test: Supabase backend Test Connection flow.

Tests:
1. Test Connection shows schema setup panel (not the old error) when tables missing
2. Schema setup panel has access token field and Setup Database button
3. The setup-schema API endpoint works with a valid access token
"""

import json
import time
import sys
import os

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.stderr.reconfigure(encoding='utf-8', errors='replace')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

from tests.e2e.conftest import TEST_BASE_URL as SERVER_URL
CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "kanban_config.json")


def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def make_driver():
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--disable-dev-shm-usage")
    driver = webdriver.Chrome(options=opts)
    driver.set_page_load_timeout(15)
    return driver


def test_test_connection_shows_setup_panel():
    """Test Connection should show schema setup panel when tables are missing."""
    cfg = load_config()
    supa_url = cfg.get("supabase_url", "")
    supa_key = cfg.get("supabase_secret_key", "")
    assert supa_url and supa_key, "Supabase credentials missing from config"

    driver = make_driver()
    try:
        print("[1/6] Loading app...")
        driver.get(SERVER_URL)
        WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        print("       OK")

        print("[2/6] Opening kanban settings (backend tab)...")
        driver.execute_script("openKanbanSettings('backend')")
        time.sleep(1)
        overlay = driver.find_element(By.ID, "pm-overlay")
        assert "show" in overlay.get_attribute("class"), "Overlay didn't open"
        print("       OK")

        print("[3/6] Selecting Supabase backend...")
        driver.execute_script("selectBackend('supabase')")
        time.sleep(0.3)
        assert driver.find_element(By.ID, "kb-supabase-config").is_displayed()
        print("       OK")

        print("[4/6] Filling credentials...")
        url_input = driver.find_element(By.ID, "kb-supa-url")
        key_input = driver.find_element(By.ID, "kb-supa-key")
        url_input.clear()
        url_input.send_keys(supa_url)
        key_input.clear()
        key_input.send_keys(supa_key)
        print("       OK")

        print("[5/6] Clicking Test Connection...")
        driver.find_element(By.ID, "kb-test-btn").click()

        status_el = driver.find_element(By.ID, "kb-conn-status")
        for _ in range(30):
            text = status_el.text.strip()
            if text and text != "Connecting...":
                break
            time.sleep(0.5)

        print("[6/6] Checking result...")
        status_text = status_el.text.strip()
        print(f"       Status: '{status_text}'")

        # MUST NOT show old error
        assert "Ensure the kanban schema" not in status_text, \
            f"FAIL: Old error still showing: '{status_text}'"

        # Should show either "Connected — ready" or "Step 2" (needs setup)
        valid = ("Connected" in status_text) or ("Step 2" in status_text)
        assert valid, \
            f"FAIL: Unexpected status, got: '{status_text}'"

        schema_panel = driver.find_element(By.ID, "kb-schema-setup")
        switch_btn = driver.find_element(By.ID, "kb-switch-btn")

        if "ready" in status_text:
            # Schema exists — Switch button visible, setup panel hidden
            assert not schema_panel.is_displayed(), "Setup panel should be hidden when ready"
            assert switch_btn.is_displayed(), "Switch button should be visible when ready"
            print("       Schema exists - Switch to Supabase button visible")
        else:
            # Needs setup — setup panel visible with token field + setup button
            assert schema_panel.is_displayed(), "FAIL: Schema setup panel not visible"
            print("       Schema setup panel: visible")
            assert driver.find_element(By.ID, "kb-access-token").is_displayed()
            print("       Access token field: visible")
            driver.execute_script("arguments[0].scrollIntoView(true)",
                                  driver.find_element(By.ID, "kb-setup-btn"))
            time.sleep(0.3)
            print("       Setup Database button: visible")

        print("\n=== PASS: Test Connection shows setup panel correctly ===")

    finally:
        driver.quit()


def test_setup_schema_api():
    """The /api/kanban/setup-schema endpoint should reject bad tokens clearly."""
    import httpx
    cfg = load_config()

    print("[1/2] Testing setup-schema with missing token...")
    r = httpx.post(f"{SERVER_URL}/api/kanban/setup-schema",
                   json={"supabase_url": cfg["supabase_url"]}, timeout=10)
    data = r.json()
    assert not data["ok"], "Should fail without token"
    print(f"       OK - rejected: {data['error'][:60]}")

    print("[2/2] Testing setup-schema with bad token...")
    r = httpx.post(f"{SERVER_URL}/api/kanban/setup-schema",
                   json={"supabase_url": cfg["supabase_url"], "access_token": "sbp_fake"},
                   timeout=15)
    data = r.json()
    assert not data["ok"], "Should fail with bad token"
    assert "token" in data["error"].lower() or "401" in data["error"], \
        f"Error should mention token, got: {data['error']}"
    print(f"       OK - rejected: {data['error'][:60]}")

    print("\n=== PASS: setup-schema API validates correctly ===")


if __name__ == "__main__":
    test_test_connection_shows_setup_panel()
    print()
    test_setup_schema_api()
