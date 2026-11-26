#!/bin/bash
set -u
B=$HOME/bench
T=$B/tools
R=$B/results
ST=$B/state
REV=c0ee4bb03e3
GEN=gen_for_loop,gen_send,next_from_c,send_value_discarded,next_value_assigned,list_of_gen,yield_from_depth5,await_chain,asyncio_pingpong,gen_finish_next,call_return,recursion_depth200,property_get,asyncio_callbacks,asyncio_short_tasks
exec >>"$B/pipeline2.log" 2>&1
stage() { echo "=== $(date -u +%FT%TZ) $*"; }

stage waiting for ppfocus
while ! [ -e "$ST/ppfocus.ok" ]; do sleep 20; done
tmux kill-session -t bench 2>/dev/null
pkill -f pyperformance 2>/dev/null
sleep 5
rm -f "$R"/pp-full-base.json "$R"/pp-full-base.log

build_variant() {
    n=$1
    cd "$B/src" && git worktree add --detach "$B/$n" $REV || return 1
    cd "$B/$n" || return 1
    if [ "$n" != base2 ]; then
    git apply "$T/exp1.diff" || return 1
    sed -i 's/^static inline _Py_CODEUNIT \*$/static inline Py_ALWAYS_INLINE _Py_CODEUNIT */' \
        Include/internal/pycore_genobject.h
    grep -q "Py_ALWAYS_INLINE _Py_CODEUNIT" Include/internal/pycore_genobject.h || return 1
    fi
    if [ "$n" = v3 ]; then
        python3 - <<'PY' || return 1
p = "Objects/genobject.c"
s = open(p).read()
k = "static PySendResult\ngen_send_ex2("
assert s.count(k) == 1
s = s.replace(k, "static inline Py_ALWAYS_INLINE PySendResult\ngen_send_ex2(")
open(p, "w").write(s)
PY
    fi
    git diff >"$R/$n.diff"
    ./configure -q --enable-optimizations --with-lto \
        --prefix="$B/inst/$n" >configure.log 2>&1 || return 1
    if [ "$n" != base2 ]; then make regen-cases >regen.log 2>&1 || return 1; fi
    make -s -j12 >build.log 2>&1 || return 1
    make -s altinstall >install.log 2>&1 || return 1
    "$B/inst/$n/bin/python3.16" -m venv "$B/venv-$n" &&
    "$B/venv-$n/bin/python" -m pip -q install pyperf >pip.log 2>&1
}

if ! [ -e "$ST/variants.ok" ]; then
    stage building v2 v3
    build_variant v2 & p1=$!
    build_variant v3 & p2=$!
    wait $p1; r1=$?
    wait $p2; r2=$?
    build_variant base2; r3=$?
    stage base2=$r3
    wait $p1; r1=$?
    wait $p2; r2=$?
    stage variants built v2=$r1 v3=$r2
    for n in v2 v3 base2; do
        [ -x "$B/inst/$n/bin/python3.16" ] || continue
        python3 "$T/instr_count.py" --python "$B/inst/$n/bin/python3.16" \
            --json "$R/instr-$n.json" >"$R/instr-$n.txt" 2>&1
        nm -S --size-sort "$B/$n/python" | grep -E "gen_send|gen_iternext|SkipDiscarded" >"$R/nm-$n.txt"
    done
    stage variant micro
    for n in base v2 v3 base2 base2 v3 v2 base; do
        [ -x "$B/venv-$n/bin/python" ] || continue
        DUALSTACK_ONLY=$GEN "$B/venv-$n/bin/python" "$T/bench_pyperf.py" -p 5 -n 3 \
            --affinity 4 --append "$R/micro2-$n.json" >>"$R/micro2-$n.txt" 2>&1
    done
    for n in v2 v3 base2; do
        "$B/venv-base/bin/python" -m pyperf compare_to "$R/micro2-base.json" \
            "$R/micro2-$n.json" --table >"$R/micro2-compare-$n.txt" 2>&1
    done
    touch "$ST/variants.ok"
fi

stage variant pyperformance focus
FOCUS=generators,coroutines,async_generators,async_tree,async_tree_io,async_tree_cpu_io_mixed,asyncio_tcp,nqueens,comprehensions
for n in v2 v3; do
    [ -e "$R/pp-focus-$n.json" ] && continue
    "$B/venv-pp/bin/pyperformance" run --python "$B/inst/$n/bin/python3.16" \
        -b "$FOCUS" -o "$R/pp-focus-$n.json" >"$R/pp-focus-$n.log" 2>&1
done
stage resuming main pipeline
exec "$T/pipeline.sh"
