#!/bin/bash
while sleep 10; do
    ps -eo pcpu,pid,args --sort=-pcpu --no-headers | awk '$1 > 20' | head -40 |
    while read cpu pid args; do
        cwd=$(readlink /proc/$pid/cwd 2>/dev/null) || continue
        exe=$(readlink /proc/$pid/exe 2>/dev/null)
        case "$cwd" in /home/ubuntu/bench*) continue;; esac
        case "$exe" in /home/ubuntu/bench*) continue;; esac
        case "$args" in *"ps -eo"*|*/home/ubuntu/bench/*) continue;; esac
        echo "$(date -u +%T) $cpu $pid cwd=$cwd $args" | cut -c1-220
    done
done >>"$HOME/bench/foreign.log" 2>&1
