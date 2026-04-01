import re
f=open("docs/photoshoot.py","r")
c=f.read()
f.close()
marker="def init_browser(driver):"
helper="""def _dismiss_overlays(driver):
    driver.execute_script(
        "var s=document.getElementById('photoshoot-overrides');"
