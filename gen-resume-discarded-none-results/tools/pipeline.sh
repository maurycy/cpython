#!/bin/bash
set -u
B=$HOME/bench
T=$B/tools
R=$B/results
ST=$B/state
REV=c0ee4bb03e3
FOCUS=generators,coroutines,async_generators,async_tree,async_tree_io,async_tree_cpu_io_mixed,asyncio_tcp,nqueens,comprehensions
mkdir -p "$R" "$ST" "$B/inst"
exec >>"$B/pipeline.log" 2>&1

stage() { echo "=== $(date -u +%FT%TZ) $*"; }
done_p() { [ -e "$ST/$1.ok" ]; }
mark() { touch "$ST/$1.ok"; }

stage start
if ! done_p worktrees; then
    cd "$B/src" || exit 1
    for n in base patch patch-dbg; do
        git worktree add --detach "$B/$n" $REV || exit 1
    done
    for n in patch patch-dbg; do
        (cd "$B/$n" && git apply "$T/exp1.diff") || exit 1
    done
    mark worktrees
fi

if ! done_p dbg; then
    stage dbg build
    cd "$B/patch-dbg" || exit 1
    ./configure -q --with-pydebug >configure.log 2>&1 &&
    make regen-cases >regen.log 2>&1 &&
    make -s -j24 >build.log 2>&1 || { stage dbg build FAILED; exit 1; }
    git diff >"$R/full-patch.diff"
    mark dbg
fi

if ! done_p tests; then
    stage tests
    cd "$B/patch-dbg" || exit 1
    ./python -m test -j20 test_generators test_coroutines test_asyncio \
        test_monitoring test_sys_settrace test_sys_setprofile test_types \
        test_yield_from test_asyncgen test_generator_stop test_contextlib \
        test_contextlib_async test_itertools test_capi test_frame \
        >"$R/tests.log" 2>&1
    echo "exit=$?" >"$R/tests.status"
    tail -5 "$R/tests.log"
    python3 "$T/instr_count.py" --python "$B/patch-dbg/python" \
        --only gen_for_loop next_from_c >"$R/smoke-instr.txt" 2>&1
    cat "$R/smoke-instr.txt"
    mark tests
fi

build_pgo() {
    n=$1
    cd "$B/$n" || return 1
    ./configure -q --enable-optimizations --with-lto \
        --prefix="$B/inst/$n" >configure.log 2>&1 || return 1
    if [ "$n" = patch ]; then
        make regen-cases >regen.log 2>&1 || return 1
    fi
    make -s -j12 >build.log 2>&1 || return 1
    make -s altinstall >install.log 2>&1 || return 1
    "$B/inst/$n/bin/python3.16" -m venv "$B/venv-$n" &&
    "$B/venv-$n/bin/python" -m pip -q install pyperf >pip.log 2>&1
}

if ! done_p pgo; then
    stage pgo builds
    build_pgo base & p1=$!
    build_pgo patch & p2=$!
    wait $p1; r1=$?
    wait $p2; r2=$?
    [ $r1 = 0 ] && [ $r2 = 0 ] || { stage pgo FAILED base=$r1 patch=$r2; exit 1; }
    for n in base patch; do
        "$B/inst/$n/bin/python3.16" -c "import sys, sysconfig; print(sys.version); print(sysconfig.get_config_var('CONFIG_ARGS'))"
    done >"$R/builds.txt"
    mark pgo
fi

if ! done_p instr; then
    stage instruction counts
    i=0
    for n in base patch patch base; do
        i=$((i + 1))
        python3 "$T/instr_count.py" --python "$B/inst/$n/bin/python3.16" \
            --json "$R/instr-$n-$i.json" >"$R/instr-$n-$i.txt" 2>&1
    done
    mark instr
fi

if ! done_p micro; then
    stage pyperf micro
    for n in base patch patch base; do
        "$B/venv-$n/bin/python" "$T/bench_pyperf.py" -p 5 -n 3 \
            --affinity 4 --append "$R/micro-$n.json" >>"$R/micro-$n.txt" 2>&1
    done
    "$B/venv-base/bin/python" -m pyperf compare_to "$R/micro-base.json" \
        "$R/micro-patch.json" --table >"$R/micro-compare.txt" 2>&1
    mark micro
fi

if ! done_p ppvenv; then
    "$B/inst/base/bin/python3.16" -m venv "$B/venv-pp" &&
    "$B/venv-pp/bin/python" -m pip -q install pyperformance >"$R/pp-pip.log" 2>&1 &&
    mark ppvenv
fi

if ! done_p ppfocus; then
    stage pyperformance focus
    for n in base patch; do
        "$B/venv-pp/bin/pyperformance" run \
            --python "$B/inst/$n/bin/python3.16" -b "$FOCUS" \
            -o "$R/pp-focus-$n.json" >"$R/pp-focus-$n.log" 2>&1
    done
    "$B/venv-base/bin/python" -m pyperf compare_to "$R/pp-focus-base.json" \
        "$R/pp-focus-patch.json" --table >"$R/pp-focus-compare.txt" 2>&1
    mark ppfocus
fi

build_extra() {
    n=$1
    cd "$B/src" && git worktree add --detach "$B/$n" $REV || return 1
    cd "$B/$n" || return 1
    case $n in v3*) git apply "$R/v3.diff" || return 1;; esac
    ./configure -q --enable-optimizations --with-lto \
        --prefix="$B/inst/$n" >configure.log 2>&1 || return 1
    case $n in v3*) make regen-cases >regen.log 2>&1 || return 1;; esac
    make -s -j8 >build.log 2>&1 || return 1
    make -s altinstall >install.log 2>&1 || return 1
    "$B/inst/$n/bin/python3.16" -m venv "$B/venv-$n" &&
    "$B/venv-$n/bin/python" -m pip -q install pyperf >pip.log 2>&1
}

if ! done_p builds3; then
    stage three builds per side
    build_extra base3 & q1=$!
    build_extra v3b & q2=$!
    build_extra v3c & q3=$!
    wait $q1; wait $q2; wait $q3
    for n in base3 v3b v3c; do
        python3 "$T/instr_count.py" --python "$B/inst/$n/bin/python3.16" \
            --json "$R/instr-$n.json" >"$R/instr-$n.txt" 2>&1
    done
    stage three builds micro
    GEN3=gen_for_loop,gen_send,next_from_c,send_value_discarded,next_value_assigned,list_of_gen,yield_from_depth5,await_chain,asyncio_pingpong,call_return,gen_finish_next
    for n in base v3 base2 v3b base3 v3c v3c base3 v3b base2 v3 base; do
        DUALSTACK_ONLY=$GEN3 "$B/venv-$n/bin/python" "$T/bench_pyperf.py" -p 5 -n 3 \
            --affinity 4 --inherit-environ DUALSTACK_ONLY \
            --append "$R/micro3-$n.json" >>"$R/micro3-$n.txt" 2>&1
    done
    mark builds3
fi

if ! done_p ppfull; then
    stage pyperformance full
    for n in base v3; do
        "$B/venv-pp/bin/pyperformance" run \
            --python "$B/inst/$n/bin/python3.16" \
            -o "$R/pp-full-$n.json" >"$R/pp-full-$n.log" 2>&1
    done
    "$B/venv-base/bin/python" -m pyperf compare_to "$R/pp-full-base.json" \
        "$R/pp-full-v3.json" --table >"$R/pp-full-compare.txt" 2>&1
    mark ppfull
fi

stage finished
