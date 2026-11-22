"""Resync GitHub step issues with the phase guides.

For every `[P<step>]` issue, replace the guide text after the header separator and the
title with the current guide content. The header (order, difficulty, dependencies) is kept.
Usage: python3 scripts/sync_issues.py [--dry-run]
"""
import json, pathlib, re, subprocess, sys, tempfile

root = pathlib.Path(__file__).resolve().parent.parent / "docs" / "phases"
step_re = re.compile(r"^## Step (\d+\.\d+) — (.*)$", re.M)
steps = {}
for g in sorted(root.glob("phase-*.md")):
    text = g.read_text()
    ms = list(step_re.finditer(text))
    for i, m in enumerate(ms):
        end = ms[i + 1].start() if i + 1 < len(ms) else text.find("## Exit Checklist")
        steps[m.group(1)] = (m.group(2).strip(), text[m.end():end].strip().rstrip("-").strip())

issues = json.loads(subprocess.run(["gh", "issue", "list", "--state", "all", "--limit", "300", "--json", "number,title,body"],
                                   capture_output=True, text=True, check=True).stdout)
dry = "--dry-run" in sys.argv
changed = 0
for it in issues:
    m = re.match(r"\[P(\d+\.\d+)\] ", it["title"])
    if not m or m.group(1) not in steps:
        continue
    sid = m.group(1); title, text = steps[sid]
    sep = "\n---\n\n"
    head = it["body"].split(sep, 1)[0]
    head = re.sub(r'(\| \*\*Guide\*\* \| `[^`]+` → "Step [\d.]+ — )[^"]*(" \|)', lambda mm: mm.group(1) + title + mm.group(2), head)
    new_body = head + sep + text + "\n"
    new_title = f"[P{sid}] {title}"
    if new_body.strip() == it["body"].strip() and new_title == it["title"]:
        continue
    changed += 1
    print(f"#{it['number']} {new_title}")
    if not dry:
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
            f.write(new_body)
        subprocess.run(["gh", "issue", "edit", str(it["number"]), "--title", new_title, "--body-file", f.name], check=True, capture_output=True)
print(f"{changed} issue(s) {'would change' if dry else 'updated'}")
