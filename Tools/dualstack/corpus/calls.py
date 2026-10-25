import sys


def leaf(x, *args, **kwargs):
    probe("leaf")
    return x + len(args) + len(kwargs)


def mid(x):
    y = leaf(x, 1, 2, k=3)
    return leaf(y)


def closure_maker(n):
    def inner(m):
        probe("inner")
        return n + m
    return inner


def recursive(n):
    if n == 0:
        probe("recursive-bottom")
        return 0
    return 1 + recursive(n - 1)


def defaults(a, b=2, *, c=3):
    return a + b + c


def star(*a, **k):
    return len(a), sorted(k)


class Methods:
    def __init__(self, v):
        self.v = v

    def m(self, x):
        return self.v + x

    @classmethod
    def c(cls, x):
        return cls(x).m(1)

    @staticmethod
    def s(x):
        return x * 2

    def __call__(self, x):
        probe("dunder-call")
        return self.m(x)


def lam():
    f = lambda x: x + 1  # noqa: E731
    return f(1) + (lambda: 2)()


print(mid(1))
print(closure_maker(2)(3))
print(recursive(5))
print(defaults(1), defaults(1, 5), defaults(1, c=9))
print(star(1, 2, x=1, y=2))
m = Methods(1)
print(m.m(2), Methods.c(3), Methods.s(4), m(5))
print(lam())
print(sys._getframe().f_code.co_name)
probe("end")
