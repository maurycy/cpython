import gc
import sys
import weakref

LOG = []


def helper(tag):
    probe(tag)
    LOG.append(tag)


class Fin:
    def __init__(self, tag):
        self.tag = tag

    def __del__(self):
        helper("del-" + self.tag)


def returns_with_local():
    x = Fin("return-local")
    return 1


returns_with_local()


def gen_with_local():
    x = Fin("gen-local")
    yield 1
    yield 2


g = gen_with_local()
next(g)
del g


def gen_finally():
    try:
        yield 1
    finally:
        helper("gen-finally")


g = gen_finally()
next(g)
del g

obj = Fin("weakref")
wr = weakref.ref(obj, lambda r: helper("weakref-callback"))
del obj


def raise_with_local():
    x = Fin("raise-local")
    raise ValueError


try:
    raise_with_local()
except ValueError:
    helper("in-except")
helper("after-except")


class Cycle:
    def __init__(self, tag):
        self.tag = tag
        self.me = self

    def __del__(self):
        helper("cycle-" + self.tag)


Cycle("a")
Cycle("b")
gc.collect()


async def coro_with_local():
    x = Fin("coro-local")
    await _never()


class _never:
    def __await__(self):
        yield


c = coro_with_local()
c.send(None)
del c
gc.collect()


def many():
    objs = [Fin(f"many-{i}") for i in range(3)]
    return len(objs)


many()
print(LOG)
probe("end")
