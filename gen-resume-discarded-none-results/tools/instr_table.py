import json, math
ks = ("base-1", "base-4", "patch-2", "patch-3")
r = {k: json.load(open(f"instr-{k}.json"))["results"] for k in ks}
print(f"{'name':28} {'base1':>9} {'base4':>9} {'patch2':>9} {'patch3':>9} {'ratio':>6}")
logs = []
for n in r["base-1"]:
    v = [r[k][n]["instructions:u"] for k in ks]
    b = (v[0] + v[1]) / 2
    p = (v[2] + v[3]) / 2
    logs.append(math.log(p / b))
    print(f"{n:28} {v[0]:9.1f} {v[1]:9.1f} {v[2]:9.1f} {v[3]:9.1f} {p/b:6.3f}")
print("geomean", round(math.exp(sum(logs) / len(logs)), 4))
