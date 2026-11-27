import statistics as st, sys
import pyperf
S = {"base": ("base1", "base2", "base3"), "v3": ("v3_1", "v3_2", "v3_3"), "fix": ("fix1", "fix2", "fix3")}
ALL = [b for s in S.values() for b in s]
D = {}
for b in ALL:
    for bm in pyperf.BenchmarkSuite.load(f"pp-focus-{b}.json"):
        D.setdefault(bm.get_name(), {})[b] = bm.mean()
print(f"{'benchmark':26}" + "".join(f"{b:>9}" for b in ALL) + "     v3    fix  fix/v3  base-spread   unit")
for n, v in sorted(D.items()):
    if len(v) < len(ALL):
        print(n, "missing", [b for b in ALL if b not in v]); continue
    u = 1e3 if max(v.values()) > 1e-3 else 1e6
    m = {s: st.mean(v[b] for b in bs) for s, bs in S.items()}
    sp = max(v[b] for b in S["base"]) / min(v[b] for b in S["base"])
    print(f"{n:26}" + "".join(f"{v[b]*u:9.2f}" for b in ALL)
          + f"  {m['v3']/m['base']:5.3f}  {m['fix']/m['base']:5.3f}  {m['fix']/m['v3']:5.3f}  {sp:5.3f}        {'ms' if u == 1e3 else 'us'}")
