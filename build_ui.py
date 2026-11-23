"""build_ui.py — inject ui_data.json into ui_template.html.

    python export_ui_data.py    # refresh the data first
    python build_ui.py          # -> procurement_desk.html

Then republish procurement_desk.html to the same artifact URL.
"""
import json

tpl = open("ui_template.html", encoding="utf-8").read()
data = json.load(open("ui_data.json", encoding="utf-8"))
payload = json.dumps(data, separators=(",", ":"), default=str)
out = tpl.replace("/*__DATA__*/", payload)
if "__DATA__" in out:
    raise SystemExit("token not replaced — check ui_template.html")
open("procurement_desk.html", "w", encoding="utf-8").write(out)
print(f"procurement_desk.html written ({len(out):,} bytes, payload {len(payload):,})")
print(f"  captured_at: {data['captured_at']}")
