import json, math, statistics as st, sys
S = {"base": ("base1", "base2", "base3"), "v3": ("v3_1", "v3_2", "v3_3"), "fix": ("fix1", "fix2", "fix3")}
ALL = [b for s in S.values() for b in s]
I = {b: json.load(open(f"instr-{b}.json"))["results"] for b in ALL}
names = list(I["base1"])
print("INSTRUCTIONS per iteration")
print(f"{'name':30}" + "".join(f"{b:>9}" for b in ALL) + "     v3    fix  fix/v3")
g = {"v3": [], "fix": []}
for n in names:
    v = {b: I[b][n]["instructions:u"] for b in ALL}
    m = {s: st.mean(v[b] for b in bs) for s, bs in S.items()}
    for s in g:
        g[s].append(math.log(m[s] / m["base"]))
    print(f"{n:30}" + "".join(f"{v[b]:9.1f}" for b in ALL)
          + f"  {m['v3']/m['base']:5.3f}  {m['fix']/m['base']:5.3f}  {m['fix']/m['v3']:5.3f}")
print("geomean", {s: round(math.exp(st.mean(x)), 4) for s, x in g.items()})
if len(sys.argv) > 1:
    sys.exit()
print()
print("TIME (us), mean per build; min-max of the nine build means for spread")
M = {}
for b in ALL:
    j = json.load(open(f"micro-{b}.json"))
    for bm in j["benchmarks"]:
        vals = [x for r in bm["runs"] for x in r.get("values", [])]
        M.setdefault(bm["metadata"]["name"].replace("micro.", ""), {})[b] = st.mean(vals) * 1e6
print(f"{'name':22}" + "".join(f"{b:>9}" for b in ALL) + "     v3    fix  fix/v3  base-spread")
for n, v in M.items():
    m = {s: st.mean(v[b] for b in bs) for s, bs in S.items()}
    sp = max(v[b] for b in S["base"]) / min(v[b] for b in S["base"])
    print(f"{n:22}" + "".join(f"{v[b]:9.2f}" for b in ALL)
          + f"  {m['v3']/m['base']:5.3f}  {m['fix']/m['base']:5.3f}  {m['fix']/m['v3']:5.3f}  {sp:5.3f}")
