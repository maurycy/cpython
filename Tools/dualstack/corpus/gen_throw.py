def leaf():
    try:
        yield "leaf"
    except KeyError:
        yield "leaf-caught"
    yield "leaf-after"


def mid(depth):
    if depth == 0:
        yield from leaf()
    else:
        yield from mid(depth - 1)


g = mid(5)
print(next(g))
probe("before-throw")
print(g.throw(KeyError("k")))
print(next(g))
print(list(g))


class Iterish:
    def __init__(self):
        self.n = 0

    def __iter__(self):
        return self

    def __next__(self):
        self.n += 1
        if self.n > 2:
            raise StopIteration("iterish-done")
        return self.n

    def throw(self, *a):
        print("Iterish.throw")
        raise StopIteration("thrown-done")

    def close(self):
        print("Iterish.close")


def delegating():
    r = yield from Iterish()
    print("delegating got", r)
    yield "end"


g = delegating()
print(next(g))
print(g.throw(ValueError))

g = delegating()
next(g)
g.close()


def closes_inner():
    try:
        yield from leaf()
    except GeneratorExit:
        print("outer GeneratorExit")
        raise


g = closes_inner()
next(g)
g.close()


def throw_before_start():
    yield 1


g = throw_before_start()
try:
    g.throw(TypeError("early"))
except TypeError as e:
    print("early throw", e, g.gi_frame)


def throw_in_finally():
    try:
        yield 1
    finally:
        print("finally runs on throw")


g = throw_in_finally()
next(g)
try:
    g.throw(IndexError)
except IndexError:
    print("propagated")


async def coro_leaf():
    try:
        await _susp()
    except KeyError:
        return "coro-caught"


class _susp:
    def __await__(self):
        yield


async def coro_mid(d):
    if d == 0:
        return await coro_leaf()
    return await coro_mid(d - 1)


c = coro_mid(4)
c.send(None)
try:
    c.throw(KeyError)
except StopIteration as e:
    print("coro throw result", e.value)
probe("end")
