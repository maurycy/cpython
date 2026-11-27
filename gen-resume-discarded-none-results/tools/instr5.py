import json, math
ks = ("base-1", "base2", "patch-2", "v2", "v3")
r = {k: json.load(open(f"instr-{k}.json"))["results"] for k in ks}
print(f"{'name':30}" + "".join(f"{k:>10}" for k in ks) + "   patch    v2    v3")
gm = {k: [] for k in ks[2:]}
for n in r["base-1"]:
    v = [r[k][n]["instructions:u"] for k in ks]
    ra = [x / v[0] for x in v[2:]]
    for k, x in zip(ks[2:], ra):
        gm[k].append(math.log(x))
    print(f"{n:30}" + "".join(f"{x:10.1f}" for x in v) + "  " + " ".join(f"{x:5.3f}" for x in ra))
print("geomean", {k: round(math.exp(sum(v) / len(v)), 4) for k, v in gm.items()})
