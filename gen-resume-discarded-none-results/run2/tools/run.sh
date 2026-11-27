#!/bin/bash
set -u
B=$HOME/bench
T=$B/tools
R=$B/results
ST=$B/state
BASE=c0ee4bb03e3
V3=28922a2ba23
FIX=a876b4ba63e
ALIGN='__attribute__((aligned(64), patchable_function_entry(16, 16)))'
TESTS="test_generators test_coroutines test_asyncio test_monitoring test_sys_settrace test_sys_setprofile test_types test_yield_from test_asyncgen test_generator_stop test_contextlib test_contextlib_async test_itertools test_capi test_frame"
MICRO=gen_for_loop,list_of_gen,next_from_c,gen_send,send_value_discarded,next_value_assigned,gen_throw_catch,gen_close_finally,yield_from_depth5,await_chain,asyncio_pingpong,gen_finish_next,call_return,recursion_depth200,property_get
FOCUS=generators,coroutines,async_generators,async_tree,async_tree_io,async_tree_cpu_io_mixed,asyncio_tcp,nqueens,comprehensions
ALL="base1 v3_1 fix1 base2 v3_2 fix2 base3 v3_3 fix3"
mkdir -p "$R" "$ST" "$B/inst"
exec >>"$B/run.log" 2>&1
stage() { echo "=== $(date -u +%FT%TZ) $*"; }
done_p() { [ -e "$ST/$1.ok" ]; }
mark() { touch "$ST/$1.ok"; }
rev_of() { case $1 in base*) echo $BASE;; v3*) echo $V3;; fix*) echo $FIX;; esac; }

stage start
if ! done_p src; then
    cd "$B" && git clone -q https://github.com/python/cpython.git src || exit 1
    cd src && git fetch -q https://github.com/maurycy/cpython.git gen-resume-guards || exit 1
    git cat-file -e $FIX && git cat-file -e $V3 && git cat-file -e $BASE || exit 1
    mark src
fi

check() {
    n=$1; shift
    cd "$B/src" && git worktree add --detach "$B/$n" $FIX || return 1
    cd "$B/$n" || return 1
    ./configure -q "$@" >configure.log 2>&1 || return 1
    make -s -j24 >build.log 2>&1 || return 1
    ./python -m test -j20 $TESTS >"$R/tests-$n.log" 2>&1
    echo "exit=$?" >"$R/tests-$n.status"
    tail -4 "$R/tests-$n.log"
}
if ! done_p tests; then
    stage tests debug; check fix-dbg --with-pydebug
    stage tests free-threaded; check fix-ft --with-pydebug --disable-gil
    stage tests tier2; check fix-t2 --with-pydebug --enable-experimental-jit=interpreter
    mark tests
fi

build() {
    n=$1
    cd "$B/src" && git worktree add --detach "$B/$n" $(rev_of $n) || return 1
    cd "$B/$n" || return 1
    sed -i "s/^Py_NO_INLINE PyObject\* _Py_HOT_FUNCTION DONT_SLP_VECTORIZE\$/$ALIGN Py_NO_INLINE PyObject* _Py_HOT_FUNCTION DONT_SLP_VECTORIZE/" Python/ceval.c
    git diff --stat | grep -q ceval.c || return 1
    ./configure -q --enable-optimizations --with-lto \
        --prefix="$B/inst/$n" >configure.log 2>&1 || return 1
    make -s -j8 >build.log 2>&1 || return 1
    make -s altinstall >install.log 2>&1 || return 1
    "$B/inst/$n/bin/python3.16" -m venv "$B/venv-$n" &&
    "$B/venv-$n/bin/python" -m pip -q install pyperf >pip.log 2>&1
}
if ! done_p builds; then
    for round in 1 2 3; do
        stage pgo round $round
        build base$round & p1=$!
        build v3_$round & p2=$!
        build fix$round & p3=$!
        wait $p1; r1=$?; wait $p2; r2=$?; wait $p3; r3=$?
        stage round $round done base=$r1 v3=$r2 fix=$r3
        [ $r1 = 0 ] && [ $r2 = 0 ] && [ $r3 = 0 ] || exit 1
    done
    for n in $ALL; do
        nm -S "$B/$n/python" | awk -v n=$n '/ _PyEval_EvalFrameDefault$/{print n, $1, $2}'
    done >"$R/addr.txt"
    for n in $ALL; do
        echo "## $n"
        nm -S --size-sort "$B/$n/python" | grep -E " gen_send|gen_iternext|SkipDiscarded"
    done >"$R/nm.txt"
    for n in $ALL; do
        "$B/inst/$n/bin/python3.16" -c "import sys, sysconfig; print('$n', sys.version, sysconfig.get_config_var('CONFIG_ARGS'))"
    done >"$R/builds.txt"
    mark builds
fi

if ! done_p instr; then
    stage instruction counts
    for n in $ALL; do
        python3 "$T/instr_count.py" --python "$B/inst/$n/bin/python3.16" \
            --json "$R/instr-$n.json" >"$R/instr-$n.txt" 2>&1
    done
    mark instr
fi

if ! done_p micro; then
    stage pyperf micro
    REVERSED=$(echo $ALL | tr ' ' '\n' | tac | tr '\n' ' ')
    for n in $ALL $REVERSED; do
        DUALSTACK_ONLY=$MICRO "$B/venv-$n/bin/python" "$T/bench_pyperf.py" -p 5 -n 3 \
            --affinity 4 --inherit-environ DUALSTACK_ONLY \
            --append "$R/micro-$n.json" >>"$R/micro-$n.txt" 2>&1
    done
    mark micro
fi

if ! done_p ppvenv; then
    "$B/inst/base1/bin/python3.16" -m venv "$B/venv-pp" &&
    "$B/venv-pp/bin/python" -m pip -q install pyperformance >"$R/pp-pip.log" 2>&1 &&
    mark ppvenv
fi

if ! done_p ppfocus; then
    stage pyperformance focus
    for n in $ALL; do
        "$B/venv-pp/bin/pyperformance" run --python "$B/inst/$n/bin/python3.16" \
            -b "$FOCUS" -o "$R/pp-focus-$n.json" >"$R/pp-focus-$n.log" 2>&1
    done
    mark ppfocus
fi

if ! done_p ppfull; then
    stage pyperformance full
    for n in base1 fix1; do
        "$B/venv-pp/bin/pyperformance" run --python "$B/inst/$n/bin/python3.16" \
            -o "$R/pp-full-$n.json" >"$R/pp-full-$n.log" 2>&1
    done
    [ -s "$R/pp-full-base1.json" ] && [ -s "$R/pp-full-fix1.json" ] && mark ppfull
    "$B/venv-base1/bin/python" -m pyperf compare_to "$R/pp-full-base1.json" \
        "$R/pp-full-fix1.json" --table >"$R/pp-full-compare.txt" 2>&1
fi
stage finished
