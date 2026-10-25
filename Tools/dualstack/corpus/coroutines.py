import types


class Awaitable:
    def __init__(self, v):
        self.v = v

    def __await__(self):
        got = yield ("await", self.v)
        return (self.v, got)


async def leaf(v):
    probe("leaf")
    r = await Awaitable(v)
    return r


async def mid(v):
    a = await leaf(v)
    b = await leaf(v + 1)
    return a, b


async def chain(n):
    if n == 0:
        probe("chain-bottom")
        return await leaf(0)
    return await chain(n - 1)


@types.coroutine
def legacy():
    x = yield "legacy"
    return x


async def uses_legacy():
    return await legacy()


async def raises():
    await Awaitable(1)
    raise ValueError("in coro")


async def catches():
    try:
        await raises()
    except ValueError as e:
        print("caught", e)
    return "ok"


def drive(coro, values=()):
    values = iter(values)
    try:
        step = coro.send(None)
        while True:
            print("yielded", step)
            step = coro.send(next(values, None))
    except StopIteration as e:
        return e.value


print(drive(mid(1), ["x", "y"]))
print(drive(chain(4), ["z"]))
print(drive(uses_legacy(), ["L"]))
print(drive(catches()))

c = chain(2)
c.send(None)
print("cr_await", type(c.cr_await).__name__, c.cr_frame.f_lineno, c.cr_running, c.cr_suspended)
probe("suspended-coro")
try:
    c.throw(KeyError("thrown"))
except KeyError as e:
    print("throw propagated", e)
print("frame after throw", c.cr_frame)

c = mid(5)
c.send(None)
c.close()
print("closed", c.cr_frame)
probe("end")
