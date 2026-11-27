#!/bin/bash
set -u
B=$HOME/bench
T=$B/tools
R=$B/results
ST=$B/state
REV=c0ee4bb03e3
exec >>"$B/pipeline6.log" 2>&1
stage() { echo "=== $(date -u +%FT%TZ) $*"; }
stage waiting for tests
while ! grep -q "finished" "$B/pipeline3.log"; do sleep 60; done

build_align() {
    n=$1; attr=$2
    cd "$B/src" && git worktree add --detach "$B/$n" $REV || return 1
    cd "$B/$n" || return 1
    sed -i "s/^Py_NO_INLINE PyObject\* _Py_HOT_FUNCTION DONT_SLP_VECTORIZE\$/$attr Py_NO_INLINE PyObject* _Py_HOT_FUNCTION DONT_SLP_VECTORIZE/" Python/ceval.c
    git diff --stat | grep -q ceval.c || return 1
    git diff >"$R/$n.diff"
    ./configure -q --enable-optimizations --with-lto \
        --prefix="$B/inst/$n" >configure.log 2>&1 || return 1
    make -s -j8 >build.log 2>&1 || return 1
    make -s altinstall >install.log 2>&1 || return 1
    "$B/inst/$n/bin/python3.16" -m venv "$B/venv-$n" &&
    "$B/venv-$n/bin/python" -m pip -q install pyperf >pip.log 2>&1
}
Q16='__attribute__((aligned(64), patchable_function_entry(16, 16)))'
if ! [ -e "$ST/align3-built.ok" ]; then
    stage building q16a q16b q16c
    build_align q16a "$Q16" & p1=$!
    build_align q16b "$Q16" & p2=$!
    build_align q16c "$Q16" & p3=$!
    wait $p1; wait $p2; wait $p3
    touch "$ST/align3-built.ok"
fi
ALL="base base2 base3 a32b o16b a32d o16c q16a q16b q16c"
for n in $ALL; do
    nm -S "$B/$n/python" | awk -v n=$n '/ _PyEval_EvalFrameDefault$/{print n, $1, $2}'
done >"$R/align3-addr.txt"
stage align micro
GEN4=call_return,recursion_depth200,property_get,gen_for_loop,gen_send,next_from_c
REVERSED=$(echo $ALL | tr ' ' '\n' | tac | tr '\n' ' ')
for n in $ALL $REVERSED; do
    DUALSTACK_ONLY=$GEN4 "$B/venv-$n/bin/python" "$T/bench_pyperf.py" -p 5 -n 3 \
        --affinity 4 --inherit-environ DUALSTACK_ONLY \
        --append "$R/micro6-$n.json" >>"$R/micro6-$n.txt" 2>&1
done
touch "$ST/align3.ok"
stage finished
