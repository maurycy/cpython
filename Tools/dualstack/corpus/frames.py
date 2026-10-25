import gc
import inspect
import sys
import traceback


def chain_names():
    f = sys._getframe()
    names = []
    while f is not None:
        names.append(f.f_code.co_name)
        f = f.f_back
    return names


def a():
    return b()


def b():
    return c()


def c():
    probe("c")
    return chain_names(), [fi.function for fi in inspect.stack()], [f.f_code.co_name for f, _ in traceback.walk_stack(None)]


print(a())


def keeps_frame():
    local = 42
    return sys._getframe()


f = keeps_frame()
print("escaped", f.f_code.co_name, f.f_locals["local"], f.f_lineno, f.f_back.f_code.co_name)
f.clear()
print("cleared", f.f_locals)
try:
    sys._getframe().clear()
except RuntimeError as e:
    print("clear running:", e)


def gen_frame():
    x = 1
    yield
    x = 2
    yield


g = gen_frame()
next(g)
fr = g.gi_frame
print("gen frame locals", fr.f_locals, fr.f_lineno)
next(g)
print("gen frame locals", fr.f_locals, fr.f_lineno)
g.close()
print("gen frame after close", g.gi_frame, fr.f_locals)


def outer():
    x = 1

    def inner():
        nonlocal x
        x += 1
        return sys._getframe().f_back.f_locals["x"]
    return inner()


print("nonlocal via f_back", outer())


def locals_proxy():
    y = 5
    fl = sys._getframe().f_locals
    fl["y"] = 7
    return y, fl["y"]


print("locals proxy", locals_proxy())


def generations():
    frames = []
    for i in range(3):
        frames.append(sys._getframe())
    return frames


fs = generations()
print("same frame object", all(x is fs[0] for x in fs))
del fs
gc.collect()

def deep(n):
    if n == 0:
        return sys._getframe()
    return deep(n - 1)


leaf = deep(30)
depth = 0
while leaf is not None:
    depth += 1
    leaf = leaf.f_back
print("depth from escaped leaf", depth)


def lineno_in_handler():
    try:
        1 / 0
    except ZeroDivisionError:
        return sys._getframe().f_lineno - lineno_in_handler.__code__.co_firstlineno


print("lineno", lineno_in_handler())
print("code positions", list(sys._getframe().f_code.co_positions())[:2])
probe("end")
