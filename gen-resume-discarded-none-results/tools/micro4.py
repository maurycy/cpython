import json, statistics, sys
ks = ("base", "base2", "v2", "v3")
d = {}
for k in ks:
    j = json.load(open(f"micro2-{k}.json"))
    for b in j["benchmarks"]:
        name = b["metadata"]["name"] if "name" in b.get("metadata", {}) else j["metadata"]["name"]
        vals = [v for r in b["runs"] for v in r.get("values", [])]
        d.setdefault(name.replace("micro.", ""), {})[k] = vals
print(f"{'name':24}" + "".join(f"{k:>20}" for k in ks) + "   base2    v2    v3   n")
for n, row in d.items():
    m = {k: statistics.mean(row[k]) for k in ks}
    s = {k: statistics.stdev(row[k]) for k in ks}
    unit = 1e6
    print(f"{n:24}" + "".join(f"{m[k]*unit:11.2f} ± {s[k]*unit:6.2f}" for k in ks)
          + "  " + " ".join(f"{m[k]/m['base']:5.3f}" for k in ks[1:]) + f"  {len(row['base'])}")
