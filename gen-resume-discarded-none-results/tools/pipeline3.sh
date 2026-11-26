#!/bin/bash
set -u
B=$HOME/bench
T=$B/tools
R=$B/results
ST=$B/state
REV=c0ee4bb03e3
TESTS="test_generators test_coroutines test_asyncio test_monitoring test_sys_settrace test_sys_setprofile test_types test_yield_from test_asyncgen test_generator_stop test_contextlib test_contextlib_async test_itertools test_capi test_frame"
exec >>"$B/pipeline3.log" 2>&1
stage() { echo "=== $(date -u +%FT%TZ) $*"; }
stage waiting for ppfull
while ! [ -e "$ST/ppfull.ok" ]; do sleep 30; done

check() {
    n=$1; shift
    cd "$B/src" && git worktree add --detach "$B/$n" $REV || return 1
    cd "$B/$n" && git apply "$R/v3.diff" || return 1
    ./configure -q "$@" >configure.log 2>&1 || return 1
    make regen-cases >regen.log 2>&1 || return 1
    make -s -j24 >build.log 2>&1 || return 1
    ./python -m test -j20 $TESTS >"$R/tests-$n.log" 2>&1
    echo "exit=$?" >"$R/tests-$n.status"
    tail -4 "$R/tests-$n.log"
}
stage v3 debug; check v3-dbg --with-pydebug
stage v3 free-threaded debug; check v3-ft --with-pydebug --disable-gil
stage v3 tier2 interpreter debug; check v3-t2 --with-pydebug --enable-experimental-jit=interpreter
stage finished
