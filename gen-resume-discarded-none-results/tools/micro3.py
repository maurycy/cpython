import json, statistics
B = ("base", "base2", "base3")
V = ("v3", "v3b", "v3c")
def load(k):
    j = json.load(open(f"micro3-{k}.json"))
    return {b["metadata"]["name"].replace("micro.", ""):
            [v for r in b["runs"] for v in r.get("values", [])] for b in j["benchmarks"]}
d = {k: load(k) for k in B + V}
ic = {k: json.load(open(f"instr-{f}.json"))["results"] for k, f in
      zip(B + V, ("base-1", "base2", "base3", "v3", "v3b", "v3c"))}
print("TIME (us), mean per build")
print(f"{'name':22}" + "".join(f"{k:>9}" for k in B + V) + "   ratio  base-spread  v3-spread")
for n in d["base"]:
    m = {k: statistics.mean(d[k][n]) * 1e6 for k in B + V}
    mb = statistics.mean(m[k] for k in B)
    mv = statistics.mean(m[k] for k in V)
    sb = max(m[k] for k in B) / min(m[k] for k in B)
    sv = max(m[k] for k in V) / min(m[k] for k in V)
    print(f"{n:22}" + "".join(f"{m[k]:9.2f}" for k in B + V) + f"   {mv/mb:5.3f}  {sb:11.3f}  {sv:9.3f}")
print()
print("INSTRUCTIONS per iteration")
print(f"{'name':22}" + "".join(f"{k:>9}" for k in B + V) + "   ratio")
for n in d["base"]:
    v = {k: ic[k][n]["instructions:u"] for k in B + V}
    mb = statistics.mean(v[k] for k in B)
    mv = statistics.mean(v[k] for k in V)
    print(f"{n:22}" + "".join(f"{v[k]:9.1f}" for k in B + V) + f"   {mv/mb:5.3f}")
