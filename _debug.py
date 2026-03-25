from pathlib import Path
import json
BASE = Path("C:/Users/15512/Documents/ClaudeGUI")
api_path = BASE / "app" / "routes" / "sessions_api.py"
text = api_path.read_text(encoding="utf-8")
le = chr(13)+chr(10) if chr(13)+chr(10) in text else chr(10)
lines = text.split(le)
for i in range(59, 79):
    print(f"LINE {i+1}: {repr(lines[i])}")
