import pathlib, re, collections
root = pathlib.Path(__file__).resolve().parent.parent / "docs"
guides = sorted((root / "phases").glob("phase-*.md"))
step_re = re.compile(r"^## Step (\d+\.\d+) — (.*)$", re.M)
code_re = re.compile(r"`([^`\n]+)`")
http_re = re.compile(r"^((?:GET|POST|PUT|DELETE|PATCH)(?:/(?:GET|POST|PUT|DELETE|PATCH))*)\s+(/\S+)$")
path_only_re = re.compile(r"^(/(?:api|id|ws|internal|mcp|sparql|health|metrics|activity|auth)(?:/\S*)?)$")
cli_re = re.compile(r"^(lae [a-z][a-z0-9-]*(?: [a-z][a-z0-9-]*)?|la-i18n [a-z]+)\b")
res_re = re.compile(r"^(linkedart://\S+)$")
rows = collections.OrderedDict()
def add(cat, key, where):
    rows.setdefault(cat, collections.OrderedDict()).setdefault(key, [])
    if where not in rows[cat][key]:
        rows[cat][key].append(where)
for g in guides:
    text = g.read_text()
    phase = re.search(r"# Phase (\d)", text).group(1)
    pos = [(m.start(), m.group(1), m.group(2)) for m in step_re.finditer(text)]
    def step_at(i):
        cur = None
        for p, n, t in pos:
            if p <= i: cur = (n, t)
        return cur
    for m in code_re.finditer(text):
        tok = m.group(1).strip()
        st = step_at(m.start())
        where = f"Phase {phase} Step {st[0]}" if st else f"Phase {phase}"
        h = http_re.match(tok)
        if h:
            add("HTTP endpoints", f"`{h.group(1)} {h.group(2)}`", where); continue
        if path_only_re.match(tok) and not tok.startswith("/ws/") and "{" not in tok.split("/")[1:2]:
            if tok.startswith("/ws"): add("WebSocket routes", f"`{tok}`", where)
            elif tok.startswith("/internal"): add("Internal RPC (crdt-ws)", f"`{tok}`", where)
            elif tok.startswith("/mcp") or tok.startswith("/sparql") or tok.startswith("/activity") or tok.startswith("/health") or tok.startswith("/metrics") or tok.startswith("/auth"): add("Other routes", f"`{tok}`", where)
            elif tok.startswith("/id"): add("Public dereference", f"`{tok}`", where)
            else: add("HTTP endpoints (method in guide text)", f"`{tok}`", where)
            continue
        if tok.startswith("/ws/"): add("WebSocket routes", f"`{tok}`", where); continue
        c = cli_re.match(tok)
        if c: add("CLI commands", f"`{c.group(1)}`", where); continue
        r = res_re.match(tok)
        if r: add("MCP resources", f"`{tok}`", where); continue
# MCP tools: explicit list from phase 3/4 guide text
tools = ["search_entities","run_sparql_readonly","get_backlinks","reconcile_entity","create_draft_record","set_preferred_name",
         "add_statement","add_classification","set_production","add_equivalent","set_place_wkt","add_comment","validate_draft",
         "preview_semantic_diff","start_job","cancel_job","propose_merge","claim_proposal","approve_merge","reject_merge",
         "escalate_to_human","claim_finding","resolve_finding","translate_record_fields"]
for g in guides:
    text = g.read_text(); phase = re.search(r"# Phase (\d)", text).group(1)
    pos = [(m.start(), m.group(1)) for m in step_re.finditer(text)]
    for t in tools:
        m = re.search(rf"`{re.escape(t)}\(", text) or re.search(rf"`{re.escape(t)}`", text)
        if m:
            cur = None
            for p, n in pos:
                if p <= m.start(): cur = n
            add("MCP tools", f"`{t}`", f"Phase {phase} Step {cur}")
out = ["# API, route, tool and command index (generated)",
       "",
       "Generated from the phase guides by `scripts/gen_api_index.py` (kept in the scratch tooling; regenerate after editing guides). Each entry names the step that introduces or extends it. The guide step is the specification for parameters and permissions.",
       ""]
order = ["HTTP endpoints", "HTTP endpoints (method in guide text)", "Public dereference", "WebSocket routes", "Internal RPC (crdt-ws)", "Other routes", "MCP resources", "MCP tools", "CLI commands"]
for cat in order:
    if cat not in rows: continue
    out.append(f"## {cat}\n")
    out.append("| Item | Introduced / extended in |")
    out.append("| :-- | :-- |")
    for k, w in sorted(rows[cat].items(), key=lambda kv: kv[0].lower()):
        out.append(f"| {k} | {', '.join(w)} |")
    out.append("")
(root / "spec" / "api-index.md").write_text("\n".join(out))
print("api-index rows:", sum(len(v) for v in rows.values()))
