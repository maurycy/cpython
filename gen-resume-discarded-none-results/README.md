# Benchmark data: skipping RESUME_CHECK and POP_TOP for a discarded None

Raw data behind the measurements of the `gen-resume-discarded-none` branch.

Setup: AMD EPYC 4484PX (Zen 4), Ubuntu 24.04.5, gcc 13.3.0, perf 6.8.12, `./configure --enable-optimizations --with-lto`, governor `performance`, benchmarks pinned to one CPU. Base is `c0ee4bb03e3`.

## Builds

| Name | Source |
| --- | --- |
| base, base2, base3 | unchanged base, three builds |
| patch | first commit of the branch (`diffs/patch.diff`, includes regenerated files) |
| v2 | patch + `Py_ALWAYS_INLINE` on `_PyGen_SkipDiscardedResume()` (`diffs/v2.diff`) |
| v3, v3b, v3c | v2 + `Py_ALWAYS_INLINE` on `gen_send_ex2()`, three builds (`diffs/v3.diff`); this is the branch head |
| a32a, a32b, o16a, o16b | unchanged base; an alignment attribute was misplaced and did not apply |
| a32c, a32d | base + `aligned(32)` on `_PyEval_EvalFrameDefault` (`diffs/aligned32.diff`) |
| o16c, o16d | base + `aligned(32)` and 16 bytes of entry padding (`diffs/aligned32-pad16.diff`) |
| q16a, q16b, q16c | base + `aligned(64)` and 16 bytes of entry padding (`diffs/aligned64-pad16.diff`) |

## Files

`json/` holds pyperf suites (`pyperf compare_to a.json b.json --table`) and instruction-count results.

| Files | Measurement |
| --- | --- |
| `json/instr-<build>*.json` | instructions, branches and branch misses per iteration, `perf stat`, 34 workloads; `-1` to `-4` are repeated runs |
| `json/micro-{base,patch}.json` | wall clock, 34 workloads, base against patch |
| `json/micro2-*.json` | wall clock, first 15 workloads, base, base2, v2, v3 |
| `json/micro3-*.json` | wall clock, 11 generator workloads, three builds per side |
| `json/micro4-*.json`, `micro5-*.json`, `micro6-*.json` | wall clock, 6 workloads, alignment experiments (10, 14 and 10 builds) |
| `json/pp-focus-*.json` | pyperformance, 9 generator and asyncio benchmarks |
| `json/pp-full-{base,v3}.json` | pyperformance, full suite |

`tables/` holds the derived tables, `nm` symbol sizes, function addresses (`align*-addr.txt`) and test logs. `tools/` holds the workloads (`instr_count.py`), the pyperf driver (`bench_pyperf.py`), the table scripts and the shell scripts that ran everything.

## Reproducing a table

```
python -m pyperf compare_to json/pp-full-base.json json/pp-full-v3.json --table
python -m pyperf compare_to json/micro3-base.json json/micro3-v3.json --table
```

## Second run

`run2/` holds a later run for the `gen-resume-guards` branch on a fresh machine, with three builds per side and a forced alignment of the interpreter loop. See `run2/README.md`.
