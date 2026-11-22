import pathlib, re, subprocess, json, time, sys, tempfile
root = pathlib.Path(__file__).resolve().parent.parent / "docs"
GUIDES = {0: "phase-0-foundations.md", 1: "phase-1-core-loop.md", 2: "phase-2-curation-vocab-scale.md",
          3: "phase-3-semantic-agents.md", 4: "phase-4-explorers-translation-admin.md"}
TITLES = {0: "Foundations & Risk Spikes", 1: "Core Authoring Loop", 2: "Curation, Vocabularies & Scale",
          3: "Semantic Search & AI Agents", 4: "Explorers, Translation & Full Operations"}

# difficulty, area labels, deps (step ids; "E0" = phase 0 epic)
META = {
 "0.1": ("M", ["infra"], []), "0.2": ("M", ["infra","a11y","i18n"], ["0.1"]), "0.3": ("M", ["infra"], ["0.1"]),
 "0.4": ("S", ["validation"], ["0.1"]), "0.5": ("M", ["db"], ["0.3","0.4"]), "0.6": ("L", ["spike","search"], ["0.3"]),
 "0.7": ("M", ["spike","crdt"], ["0.1"]), "0.8": ("XL", ["spike","crdt"], ["0.4"]), "0.9": ("M", ["spike","validation"], ["0.4"]),
 "0.10": ("S", ["spike","frontend"], ["0.1"]), "0.11": ("M", ["spike","infra"], ["0.3","0.5"]), "0.12": ("M", ["backend"], ["0.1"]),
 "1.1": ("M", ["backend"], ["E0"]), "1.2": ("S", ["backend"], ["1.1"]), "1.3": ("XL", ["backend"], ["1.2","0.6"]),
 "1.4": ("XL", ["backend","crdt"], ["1.2","0.8"]), "1.5": ("M", ["backend","db"], ["1.2"]), "1.6": ("L", ["backend"], ["1.3","1.4","1.5"]),
 "1.7": ("M", ["backend"], ["1.6"]), "1.8": ("XL", ["backend","crdt"], ["1.4","1.7","0.7"]), "1.9": ("XL", ["backend","validation"], ["1.3","1.4","0.9"]),
 "1.10": ("XL", ["backend","db"], ["1.8","1.9","1.5"]), "1.11": ("XL", ["backend","search"], ["1.10","1.3"]), "1.12": ("L", ["backend","infra"], ["1.3","1.5","0.11"]),
 "1.13": ("M", ["backend","search"], ["1.12","0.6"]), "1.14": ("L", ["backend"], ["1.4","1.8"]), "1.15": ("S", ["backend"], ["1.8"]),
 "1.16": ("M", ["frontend"], ["1.7","0.2"]), "1.17": ("L", ["frontend","crdt"], ["1.16","1.8"]), "1.18": ("XL", ["frontend"], ["1.17","1.13","0.12"]),
 "1.19": ("M", ["frontend","validation"], ["1.18","1.9","1.10","1.14"]), "1.20": ("S", ["frontend"], ["1.19"]), "1.21": ("M", ["backend","frontend","a11y"], ["1.6","0.10"]),
 "1.22": ("M", ["infra"], ["1.19","1.21","1.12"]),
 "2.1": ("L", ["backend","db"], ["E1"]), "2.2": ("M", ["backend","validation"], ["2.1"]), "2.3": ("M", ["frontend"], ["2.2"]),
 "2.4": ("M", ["frontend","backend"], ["2.3"]), "2.5": ("M", ["backend","search"], ["2.2"]), "2.6": ("XL", ["backend","db"], ["2.5"]),
 "2.7": ("L", ["backend","frontend","search"], ["2.2"]), "2.8": ("L", ["frontend","backend","db"], ["2.7"]), "2.9": ("L", ["backend","frontend","db"], ["2.1"]),
 "2.10": ("S", ["backend","frontend"], ["2.9"]), "2.11": ("M", ["frontend","a11y"], ["2.3"]), "2.12": ("L", ["backend","frontend"], ["2.11"]),
 "2.13": ("S", ["frontend"], ["2.1"]), "2.14": ("L", ["backend","infra","db"], ["2.1"]), "2.15": ("L", ["backend","frontend"], ["2.8"]),
 "2.16": ("M", ["backend"], ["2.5"]), "2.17": ("L", ["backend","ops","db"], ["2.12"]), "2.18": ("XL", ["backend","ops"], ["2.17"]),
 "2.19": ("M", ["infra","ops"], ["2.17"]), "2.20": ("M", ["infra"], ["2.14","2.18"]),
 "3.1": ("L", ["backend","db","search"], ["E2"]), "3.2": ("M", ["backend","frontend","search"], ["3.1"]), "3.3": ("L", ["backend","search"], ["3.1"]),
 "3.4": ("L", ["backend","agents","db"], ["E2"]), "3.5": ("M", ["backend","agents"], ["3.4"]), "3.6": ("XL", ["backend","agents","crdt"], ["3.5"]),
 "3.7": ("M", ["backend","agents","db"], ["3.5"]), "3.8": ("XL", ["backend","agents","db"], ["3.6","3.7"]), "3.9": ("M", ["agents"], ["3.8","3.3","3.12"]),
 "3.10": ("L", ["backend","frontend","db"], ["3.7"]), "3.11": ("L", ["backend"], ["3.5"]), "3.12": ("L", ["backend","agents","db"], ["3.4"]),
 "3.13": ("XL", ["backend","frontend","agents"], ["3.6","3.12"]),
 "4.1": ("M", ["backend","frontend","search"], ["E3"]), "4.2": ("L", ["frontend","backend","a11y"], ["4.1"]), "4.3": ("S", ["frontend"], ["4.2"]),
 "4.4": ("M", ["frontend","backend"], ["4.1"]), "4.5": ("M", ["frontend","backend"], ["4.1"]), "4.6": ("L", ["backend","frontend","search"], ["4.1"]),
 "4.7": ("S", ["frontend"], ["4.6"]), "4.8": ("M", ["backend","agents"], ["E3"]), "4.9": ("L", ["i18n","backend"], ["4.8"]),
 "4.10": ("L", ["backend","frontend","i18n"], ["4.8"]), "4.11": ("S", ["backend","db"], ["4.10"]), "4.12": ("XL", ["backend","frontend","db"], ["E3"]),
 "4.13": ("M", ["backend","frontend"], ["4.12"]), "4.14": ("L", ["backend","frontend"], ["4.12"]), "4.15": ("XL", ["backend","frontend","crdt"], ["E3"]),
 "4.16": ("L", ["backend","frontend","ops","db"], ["E3"]), "4.17": ("M", ["backend","ops","db"], ["4.16"]), "4.18": ("M", ["backend","ops"], ["4.16"]),
 "4.19": ("L", ["a11y","infra"], ["4.7","4.11","4.14","4.17","4.18"]),
}
DIFF_HOURS = {"S": "about half a day", "M": "1–2 days", "L": "3–5 days", "XL": "1–2 weeks; critical"}

def run(args, inp=None):
    for attempt in range(5):
        r = subprocess.run(args, capture_output=True, text=True, input=inp)
        if r.returncode == 0:
            return r.stdout.strip()
        print("retry", attempt, args[:3], r.stderr.strip()[:120], file=sys.stderr)
        time.sleep(5 * (attempt + 1))
    raise SystemExit(1)

# ---- labels
LABELS = {"phase-0": "0e8a16", "phase-1": "1d76db", "phase-2": "5319e7", "phase-3": "d93f0b", "phase-4": "b60205",
          "difficulty:S": "c2e0c6", "difficulty:M": "fbca04", "difficulty:L": "f9a825", "difficulty:XL": "e11d21",
          "critical-model": "000000", "epic": "3e4b9e",
          "area:backend": "bfdadc", "area:frontend": "c5def5", "area:infra": "d4c5f9", "area:db": "fef2c0", "area:spike": "e99695",
          "area:agents": "f7c6c7", "area:i18n": "bfd4f2", "area:a11y": "f143ab", "area:ops": "ededed", "area:search": "0052cc",
          "area:crdt": "006b75", "area:validation": "5319e7"}
existing = set(l["name"] for l in json.loads(run(["gh","label","list","--limit","200","--json","name"])))
for name, color in LABELS.items():
    if name not in existing:
        run(["gh","label","create",name,"--color",color,"--force"])
print("labels ok")

# ---- parse guides
steps = {}   # id -> (title, body, phase, critical)
exits = {}
step_re = re.compile(r"^## Step (\d+\.\d+) — (.*)$", re.M)
for ph, fn in GUIDES.items():
    text = (root/"phases"/fn).read_text()
    ms = list(step_re.finditer(text))
    for i, m in enumerate(ms):
        end = ms[i+1].start() if i+1 < len(ms) else text.find("## Exit Checklist")
        body = text[m.end():end].strip().rstrip("-").strip()
        steps[m.group(1)] = (m.group(2).strip(), body, ph, "CRITICAL STEP" in body)
    ex = text[text.find("## Exit Checklist"):text.find("## Common Mistakes")].strip()
    exits[ph] = ex
assert set(steps) == set(META), set(steps) ^ set(META)

numbers = {}  # step id or "E<ph>" -> issue number
order = 0
for it in json.loads(run(["gh","issue","list","--state","all","--limit","300","--json","number,title"])):
    t = it["title"]
    m = re.match(r"\[P(\d+\.\d+)\]", t)
    if m: numbers[m.group(1)] = it["number"]
    m = re.match(r"\[Phase (\d)\]", t)
    if m: numbers[f"E{m.group(1)}"] = it["number"]
order = len(numbers)
print("resuming with", order, "existing issues")
def create(title, body, labels):
    global order
    order += 1
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
        f.write(body); path = f.name
    url = run(["gh","issue","create","--title",title,"--body-file",path,"--label",",".join(labels)])
    num = int(url.rstrip("/").split("/")[-1])
    time.sleep(1.2)
    return num

def step_key(s):
    a, b = s.split("."); return (int(a), int(b))

for ph in range(5):
    ids = sorted([s for s in steps if steps[s][2] == ph], key=step_key)
    # topological order within the phase (deps first), step-number tie-break
    done, topo = set(), []
    while len(topo) < len(ids):
        for sid in ids:
            if sid in done: continue
            if all((d not in ids) or (d in done) for d in META[sid][2]):
                topo.append(sid); done.add(sid); break
        else:
            raise SystemExit("cycle in deps for phase %d" % ph)
    for sid in topo:
        title, body, _, critical = steps[sid]
        diff, areas, deps = META[sid]
        dep_refs = ", ".join(f"#{numbers[d]}" for d in deps) or "none"
        model = "**larger model required** (Claude Fable/Mythos class; see `docs/conventions.md` §10)" if critical else "Claude Opus 5.5"
        labels = [f"phase-{ph}", f"difficulty:{diff}"] + [f"area:{a}" for a in areas] + (["critical-model"] if critical else [])
        if sid in numbers:
            continue
        head = (f"| | |\n| :-- | :-- |\n| **Order** | {order+1} |\n| **Phase** | {ph} — {TITLES[ph]} |\n| **Step** | {sid} |\n"
                f"| **Difficulty** | {diff} ({DIFF_HOURS[diff]}) |\n| **Model** | {model} |\n| **Depends on** | {dep_refs} |\n"
                f"| **Guide** | `docs/phases/{GUIDES[ph]}` → \"Step {sid} — {title}\" |\n| **Spec & decisions** | `docs/spec/README.md`, `docs/decisions/REGISTER.md` |\n\n"
                f"Work this issue exactly as the guide step below. A step is done when **Verify** passes, CI is green, the commit exists, "
                f"and the progress board in `docs/README.md` is updated in the same PR.\n\n---\n\n")
        full = head + body + "\n"
        numbers[sid] = create(f"[P{sid}] {title}", full, labels)
        print(sid, "->", numbers[sid], flush=True)
    # epic
    checklist = "\n".join(f"- [ ] #{numbers[s]} Step {s} — {steps[s][0]}" for s in ids)
    prev = f"Requires #{numbers[f'E{ph-1}']} (Phase {ph-1} tracking issue) to be closed first.\n\n" if ph > 0 else ""
    ebody = (f"Tracking issue for **Phase {ph} — {TITLES[ph]}**. Guide: `docs/phases/{GUIDES[ph]}`. Roadmap: `docs/implementation_phases.md`.\n\n"
             f"{prev}## Steps (in dependency order)\n\n{checklist}\n\n{exits[ph]}\n\nClose this issue only when every step above is closed and every exit item is ticked.\n")
    if f"E{ph}" not in numbers:
        numbers[f"E{ph}"] = create(f"[Phase {ph}] {TITLES[ph]} — tracking", ebody, [f"phase-{ph}", "epic"])
    print(f"E{ph} ->", numbers[f'E{ph}'], flush=True)
json.dump(numbers, open(root.parent / ".issue-map.json", "w"), indent=1)
print("created", order, "issues")
