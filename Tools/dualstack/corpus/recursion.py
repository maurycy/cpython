import sys

sys.setrecursionlimit(300)


def rec(n):
    try:
        return rec(n + 1)
    except RecursionError:
        return n


d1 = rec(0)
d2 = rec(0)
print("same depth twice", d1 == d2)


def handler_work():
    try:
        rec_nocatch(0)
    except RecursionError:
        probe("in-handler")
        return small(20)


def rec_nocatch(n):
    return rec_nocatch(n + 1)


def small(n):
    return small(n - 1) if n else "ok"


print("work in handler", handler_work())


def gen_rec(n):
    yield from gen_rec(n + 1)


try:
    next(gen_rec(0))
except RecursionError:
    print("gen recursion error")


class Init:
    def __init__(self):
        Init()


try:
    Init()
except RecursionError:
    print("init recursion error")


def limit_mid(n):
    if n == 100:
        sys.setrecursionlimit(600)
    return limit_mid(n + 1)


try:
    limit_mid(0)
except RecursionError:
    print("raised limit mid-flight ok")
sys.setrecursionlimit(300)


def via_eval(n):
    return eval("via_eval(n + 1)")


try:
    via_eval(0)
except RecursionError:
    print("eval recursion error")


async def coro_rec(n):
    return await coro_rec(n + 1)


c = coro_rec(0)
try:
    c.send(None)
except RecursionError:
    print("coro recursion error")
c.close()

print("depth after all", rec(0) == d1)
probe("end")
