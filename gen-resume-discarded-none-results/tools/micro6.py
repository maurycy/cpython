import json, statistics
ks = "base base2 base3 a32b o16b a32d o16c q16a q16b q16c".split()
addr = {l.split()[0]: int(l.split()[1], 16) for l in open("align3-addr.txt")}
d = {}
for k in ks:
    j = json.load(open(f"micro6-{k}.json"))
    for b in j["benchmarks"]:
        vals = [v for r in b["runs"] for v in r.get("values", [])]
        d.setdefault(b["metadata"]["name"].replace("micro.", ""), {})[k] = statistics.mean(vals) * 1e6
print(f"{'build':8}{'mod64':>6}" + "".join(f"{w:>20}" for w in d))
for k in ks:
    print(f"{k:8}{addr[k] % 64:6d}" + "".join(f"{d[w][k]:20.2f}" for w in d))
