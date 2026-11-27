# Second run: the `gen-resume-guards` branch

Raw data for the measurements of `gen-resume-guards`, taken on a freshly installed machine of the same type (AMD EPYC 4484PX, Ubuntu 24.04.5, gcc 13.3.0, perf 6.8.12, `./configure --enable-optimizations --with-lto`).

## Builds

Three PGO+LTO builds per side, nine in total.

| Name | Source |
| --- | --- |
| base1, base2, base3 | `c0ee4bb03e3` |
| v3_1, v3_2, v3_3 | `28922a2ba23`, head of `gen-resume-discarded-none` |
| fix1, fix2, fix3 | `a876b4ba63e`, head of `gen-resume-guards` |

Every build carries `diffs/alignment.diff`, which is in neither branch. It places `_PyEval_EvalFrameDefault` at 16 modulo 64 in all nine builds (`tables/addr.txt`), so that they fall into the same speed class.

## Files

| Files | Measurement |
| --- | --- |
| `json/instr-<build>.json` | instructions, branches and branch misses per iteration, `perf stat`, 35 workloads |
| `json/micro-<build>.json` | wall clock, 15 workloads, 10 processes x 3 values per build |
| `json/pp-focus-<build>.json` | pyperformance, 9 generator and asyncio benchmarks, all nine builds |
| `json/pp-full-{base1,fix1}.json` | pyperformance, full suite |

`tables/` holds the derived tables (`tables.txt`, `pp-focus-table.txt`, `pp-full-compare.txt`), `nm` symbol sizes, build information and the test logs of three debug builds of `a876b4ba63e` (default, free-threaded, tier 2 interpreter). `tools/` holds `run.sh`, which ran everything, the workloads, the table scripts and `watch.sh`, which logged any process outside the benchmark using more than 20% of a CPU.

```
python -m pyperf compare_to json/pp-full-base1.json json/pp-full-fix1.json --table
python -m pyperf compare_to json/micro-v3_1.json json/micro-fix1.json --table
```
