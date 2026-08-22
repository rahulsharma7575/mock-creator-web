#!/usr/bin/env python3
import re, pathlib, datetime, sys
p = pathlib.Path(__file__).resolve().parents[1] / "PROJECT_CONTEXT.md"
if not p.exists():
    print("PROJECT_CONTEXT.md not found")
    sys.exit(1)
t = p.read_text(encoding="utf-8")
m = re.search(r"Version:\s*v(\d+)", t)
v = int(m.group(1)) + 1 if m else 1
today = datetime.date.today().isoformat()
msg = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "update"
t = re.sub(r"Version:\s*v\d+.*", f"Version: v{v} — {today}", t, count=1)
t = re.sub(r"\*\*Latest:\*\*.*", f"**Latest:** {msg} (v{v})", t, count=1)
# enforce 500 words
words = len(t.split())
if words > 500:
    print(f"WARNING: {words} words >500, please trim")
    sys.exit(1)
p.write_text(t, encoding="utf-8")
print(f"bumped to v{v} ({words} words)")
