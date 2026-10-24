import gc
import sys


def counter(n):
    for i in range(n):
        got = yield i
        if got is not None:
            print("got", got)
    return "done"


def delegate(n):
    result = yield from counter(n)
    print("delegate result", result)
    yield "after"


def deep(n):
    if n == 0:
        probe("deep-bottom")
        yield "bottom"
    else:
        yield from deep(n - 1)


def with_finally():
    try:
        yield 1
        yield 2
    finally:
        print("finally ran")


def catches_throw():
    try:
        yield 1
    except ValueError as e:
        print("caught", e)
        yield 2
    yield 3


def bad_close():
    try:
        yield 1
    finally:
        yield 2  # RuntimeError on close


g = counter(3)
print(next(g), g.send("x"), next(g))
try:
    next(g)
except StopIteration as e:
    print("stop", e.value)

print(list(delegate(2)))
print(list(deep(5)))

g = with_finally()
print(next(g))
g.close()
print("closed", g.gi_frame)

g = catches_throw()
print(next(g), g.throw(ValueError("boom")), next(g))

g = bad_close()
next(g)
try:
    g.close()
except RuntimeError as e:
    print("runtime", e)

g = counter(2)
next(g)
print("suspended lineno", g.gi_frame.f_lineno, g.gi_suspended, g.gi_running)
print("yieldfrom", g.gi_yieldfrom)
d = delegate(3)
next(d)
print("delegating to", type(d.gi_yieldfrom).__name__, d.gi_yieldfrom.gi_frame.f_lineno)
probe("suspended")
del d
del g
gc.collect()

genexp = (x * 2 for x in range(3))
print(list(genexp))
print(sum(x for x in range(10)))


def leaks_frame():
    yield sys._getframe()


f = next(leaks_frame())
print("frame after gen dropped", f.f_code.co_name, f.f_back is None or f.f_back.f_code.co_name)


def inner_exc():
    yield 1
    raise KeyError("k")


def outer_exc():
    try:
        yield from inner_exc()
    except KeyError as e:
        print("outer caught", e)
        yield "recovered"


print(list(outer_exc()))
probe("end")
