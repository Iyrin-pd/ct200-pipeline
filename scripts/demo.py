"""End-to-end demo: v1 -> selections -> generation -> v2 re-ingest -> staleness.

Usage (server must be running on a FRESH database):
    python scripts/demo.py [base_url]
"""
import sys

import httpx

sys.stdout.reconfigure(encoding="utf-8")        # Windows consoles choke on the manual's special hyphens
BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
c = httpx.Client(base_url=BASE, timeout=120)


def step(title):
    print(f"\n=== {title} ===")


def ok(r, *codes):
    if r.status_code not in (codes or (200, 201)):
        print("UNEXPECTED RESPONSE", r.status_code, r.text[:600])
        sys.exit(1)
    return r.json()


def node_id(number, version):
    tree = ok(c.get("/documents/ct200/tree", params={"version": version}))["tree"]
    stack = [tree]
    while stack:
        n = stack.pop()
        if n["number"] == number:
            return n["id"]
        stack.extend(n["children"])
    sys.exit(f"section {number} not found in version {version}")


def show_cases(gen):
    summary = gen["staleness_summary"]
    print(f"  generation {gen['id'][:8]} | status={gen['status']} | model={gen['model']} | overall staleness={summary['overall']} | counts={summary['counts']}")
    for tc in gen["test_cases"]:
        st = tc["staleness"]
        line = f"   - [{st['status']:<16}] {tc['title'][:70]}"
        print(line)
        for reason in st["reasons"][:2]:
            print(f"        why: {reason[:110]}")


step("0. Health + clean-database check")
ok(c.get("/health"))
if c.get("/documents").json():
    sys.exit("Database is not empty. Stop the server, delete the 'var' folder, restart the server, run again.")
print("server is up, database is empty")

step("1. Ingest v1")
v1 = ok(c.post("/documents/ct200/versions", json={"path": "ct200_manual.pdf"}))
print(f"  version {v1['version_no']}: {v1['node_count']} nodes, parse warnings: {v1['parse_warnings']}")
dups = ok(c.get("/documents/ct200/search", params={"q": "error codes", "scope": "heading"}))["results"]
print("  duplicate heading 'Error Codes' -> distinct nodes:", [(d["number"], d["id"]) for d in dups])

step("2. Create two version-pinned selections from v1")
sel_a = ok(c.post("/selections", json={"name": "inflation + error codes", "node_ids": [node_id("3.2", 1), node_id("4.2", 1)]}))
sel_b = ok(c.post("/selections", json={"name": "storage + bluetooth (will not change)", "node_ids": [node_id("5.1", 1), node_id("5.2", 1)]}))
print(f"  selection A = id {sel_a['id']} (sections 3.2, 4.2)   selection B = id {sel_b['id']} (sections 5.1, 5.2)")

step("3. Generate test cases from both (v1 text) - everything should be current")
gen_a = ok(c.post(f"/selections/{sel_a['id']}/generate"))
gen_b = ok(c.post(f"/selections/{sel_b['id']}/generate"))
show_cases(gen_a)
show_cases(gen_b)

step("4. Same request again -> idempotent, no new LLM call")
again = ok(c.post(f"/selections/{sel_a['id']}/generate"), 200)
print("  reused:", again["reused"], "| same generation id:", again["id"] == gen_a["id"])

step("5. Re-ingest the modified manual as v2")
v2 = ok(c.post("/documents/ct200/versions", json={"path": "ct200_manual_v2.pdf"}))
s = v2["summary"]
print(f"  version {v2['version_no']}: {v2['node_count']} nodes")
print("  added   :", [a["number"] for a in s["added"]])
print("  modified:", [(m["number"], m["change_class"]) for m in s["modified"]])
print("  removed :", s["removed"], "| unchanged sections:", s["unchanged"])

step("6. What changed in section 4.2? (asked via the v1 node id)")
ch = ok(c.get(f"/nodes/{node_id('4.2', 1)}/changes"))
print("  status:", ch["status"], "| class:", ch["diff"]["change_class"], "| same logical section across versions:", [h["version_no"] for h in ch["history"]])
for nc in ch["diff"]["numeric_changes"]:
    print(f"   numeric change: {nc['old']!r} -> {nc['new']!r}")

step("7. Selection A is still pinned to the exact v1 text")
pinned = ok(c.get(f"/selections/{sel_a['id']}"))
for item in pinned["items"]:
    print(f"  {item['number']} (v{item['version_no']}) latest_state={item['latest_state']} | contains '40 mmHg': {'40 mmHg' in item['text']}")

step("8. Retrieve the SAME test cases again - staleness is evaluated now, against v2")
print(" Selection A (touches changed sections):")
show_cases(ok(c.get(f"/selections/{sel_a['id']}/test-cases"))["generations"][0])
print(" Selection B (sections unchanged):")
show_cases(ok(c.get(f"/selections/{sel_b['id']}/test-cases"))["generations"][0])

step("9. Same data via a node id of the NEW version (matched by logical identity)")
by_node = ok(c.get(f"/nodes/{node_id('4.2', 2)}/test-cases"))
print(f"  {by_node['count']} generation(s) cite section 4.2; cases citing it: {sum(len(g['test_cases']) for g in by_node['generations'])}")

print("\nDemo finished: versioning + staleness flow works end to end.")