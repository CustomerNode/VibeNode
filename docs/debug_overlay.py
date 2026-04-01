import sys, io, time
sys.stdout=io.TextIOWrapper(sys.stdout.buffer,encoding="utf-8",errors="replace")
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
opts=Options()
for a in ["--headless=new","--window-size=1920,1080","--force-device-scale-factor=1","--disable-gpu","--no-sandbox","--hide-scrollbars"]:opts.add_argument(a)
driver=webdriver.Chrome(options=opts)
try:
 driver.get("http://localhost:5050")
 time.sleep(2)
 driver.execute_script("localStorage.setItem(\"activeProject\",\"C--Users-15512-Documents-VibeNode\");localStorage.setItem(\"theme\",\"dark\");localStorage.setItem(\"viewMode\",\"grid\");")
 driver.get("http://localhost:5050")
 time.sleep(5)
 ci=driver.execute_script("var el=document.elementFromPoint(960,540);if(!el)return\"none\";var c=[];var cur=el;while(cur&&cur!==document.documentElement){var cs=getComputedStyle(cur);c.push({t:cur.tagName,i:cur.id||\"_\",cn:(cur.className&&typeof cur.className===\"string\")?cur.className.substring(0,100):\"_\",z:cs.zIndex,p:cs.position,d:cs.display,w:cur.offsetWidth,h:cur.offsetHeight});cur=cur.parentElement;}return c;")
 print("CENTER:");[print(" "+str(x))for x in(ci if isinstance(ci,list)else[ci])]
 ov=driver.execute_script("var ids=[\"project-overlay\",\"health-blocker\",\"pm-overlay\",\"extract-drawer\",\"compare-overlay\"];var r=[];ids.forEach(function(id){var el=document.getElementById(id);if(el){var cs=getComputedStyle(el);r.push({id:id,d:cs.display,s:el.classList.contains(\"show\"),o:el.classList.contains(\"open\"),z:cs.zIndex});}else r.push({id:id,m:1});});return r;")
 print("OVERLAYS:");[print(" "+str(x))for x in ov]
 ct=driver.execute_script("var el=document.elementFromPoint(960,540);if(!el)return\"none\";var c=el;for(var i=0;i<5;i++){if(c.innerText&&c.innerText.trim().length>0)return c.innerText.substring(0,300);c=c.parentElement;if(!c)break;}return el.tagName;")
 print("TEXT:",ct)
finally:driver.quit()
