# Dual-stack verifiers

Fast, deterministic checks for work on the control/data stack split
(gh-157559). They complement the test suite and the sampling oracle
(`Tools/inspection/oracle_external_inspection.py`), which is statistical and
slow.

| # | What | Where | Cost |
|---|------|-------|------|
| 1 | Control stack invariant checker (debug builds) | `Python/pystate.c:_PyThreadState_CheckControlStack`, hooks in `pycore_interpframe.h`, `_testinternalcapi.check_control_stack()` / `control_stack_dump()` | every push/pop in `Py_DEBUG` |
| 2 | Recursion depth table | `recursion_table.py` | < 1 s |
| 3 | Golden frame-event traces over a corpus | `frame_trace.py`, `corpus/`, `golden/` | seconds |
| 4 | Instruction counts per operation | `instr_count.py` (perf stat or cachegrind, Linux) | seconds |
| 5 | Oracle quick mode | `oracle_quick.py` | ~10 s |
| 6 | Stackref-debug + refleak runs | `refleak.sh` | minutes |
| 7 | pyperf wall clock on the same workloads, PGO+LTO builds only | `bench_pyperf.py` | minutes |

`verify.sh quick` runs 1-3 (and 4 when `perf` exists); `verify.sh full` adds 5-6.

## 1. Invariant checker

Compiled into every build, invoked automatically in `Py_DEBUG` builds on each
control record push/pop (window of the top 8 records) and from Python via
`_testinternalcapi.check_control_stack(strict=False, window=False)`.
`PYTHON_CONTROLSTACK_CHECK=full|window|off` selects the automatic mode.
Popped records are poisoned in debug builds (`owner = 0x5d`, `data = 0xDD..`).

Invariants (ids appear in error messages):

- H1 `base < top <= end <= reservation`
- H2 `limit == base + min(capacity, 1 + recursion_limit + extra [+ 50])`
- H3 `extra >= 0`
- B1 slot 0 is `CONTROL_BASE`, owner `INTERPRETER`, `data == &base_data`
- C1 `current_frame` in `[base, top)` and `ACTIVE` (or `BASE`)
- C2 no `ACTIVE` record above `current_frame`
- R1 no `BASE` above slot 0; `DETACHED` only for a dying generator
- R2 owner is one of the four known owners (poison is 0x5d)
- T1 thread-owned `data` lies in a data stack chunk
- T2 topmost thread-owned `data` lies below `datastack_top`, in the current chunk or the one before it (a dying frame's data outlives its control record)
- G1 `gen->gi_live_frame == record`, `gi_iframe` stays `DETACHED`, `gi_iframe.data == &gi_data`
- G2 `gi_frame_state` is `EXECUTING` while linked (`CLEARED` when `DETACHED`)
- E1 `ACTIVE` entry records point `data` into this thread's C stack
- F1 `data->frame_obj->f_frame == record`
- S1 (debug) `stackpointer` within `[localsplus, localsplus + nlocalsplus + stacksize]`
- X1 `extra >= #entry records` (`==` in strict mode and whenever the stack is empty)
- P1 (debug) slots above `top` are neither `ACTIVE` nor `RESERVED`

Strict mode is only meaningful from a plain Python frame: `gen.throw()` and
`__init__` shims legitimately bump `extra` while they run.

## 2. Recursion depth table

```
./python.exe Tools/dualstack/recursion_table.py --json out.json
./python.exe Tools/dualstack/recursion_table.py --compare golden/recursion_main.json
```

`golden/recursion_proto.json` is the unmodified prototype (what `verify.sh` compares against); `golden/recursion_baseline.json` is the prototype's parent commit.

Each scenario runs in a fresh interpreter with `sys.setrecursionlimit(1000)`
and reports the depth at which `RecursionError` fires, or `CRASH`. Any change
versus the baseline is an accounting change in `controlstack_extra`,
`AdjustControlLimit` or the entry/shim pushes.

## 3. Golden frame-event traces

```
./python.exe Tools/dualstack/frame_trace.py record --out golden/   # with the baseline python
./python.exe Tools/dualstack/frame_trace.py compare golden/
```

Runs every `corpus/*.py` under `sys.monitoring` and records PY_START,
PY_RESUME, PY_RETURN, PY_YIELD, PY_UNWIND, PY_THROW, RAISE, RERAISE,
EXCEPTION_HANDLED, STOP_ITERATION for corpus frames, plus program stdout and
`probe()` snapshots (stack of corpus frames via `sys._getframe`). The `vm`
section (control stack dump) is compared only when both sides have it.

## 4. Instruction counts

```
./python Tools/dualstack/instr_count.py --backend perf --json out.json
./python Tools/dualstack/instr_count.py --backend perf --compare golden/instr_main.json
```

Counts `instructions:u` for each workload at two sizes and reports the
per-iteration slope, so process startup cancels out. Deterministic to well
under 1% on the same binary; use it for "did this change add instructions per
return", not instead of PGO+LTO wall-clock numbers.

## 5. Oracle quick

```
sudo ./python.exe Tools/dualstack/oracle_quick.py
```

One short blocking-nocache and live-nocache run per snippet. Fails when the
blocking reference reports errors or impossible stacks.

## 7. pyperf (milestones only)

```
./python Tools/dualstack/bench_pyperf.py -o proto.json     # PGO+LTO build
./python -m pyperf compare_to base.json proto.json --table
```

Same workloads as instr_count.py. Not part of verify.sh: wall clock on a
debug or plain build is not a measurement we report.

## 6. Refleaks

```
Tools/dualstack/refleak.sh            # -R 3:3 on the frame-heavy tests
Tools/dualstack/refleak.sh --stackref # also a Py_STACKREF_DEBUG build in a detached worktree
```
