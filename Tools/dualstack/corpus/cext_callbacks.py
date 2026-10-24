import functools
import re


def key(x):
    probe("key")
    return -x


print(sorted([3, 1, 2], key=key))
print(list(map(lambda x: x * 2, [1, 2, 3])))
print(functools.reduce(lambda a, b: a + b, [1, 2, 3, 4]))
print(re.sub(r"\d", lambda m: str(int(m.group()) + 1), "a1b2"))
print(list(filter(lambda x: x % 2, range(6))))


class Iter:
    def __init__(self, n):
        self.i = 0
        self.n = n

    def __iter__(self):
        return self

    def __next__(self):
        if self.i >= self.n:
            raise StopIteration
        self.i += 1
        return self.i


print(list(Iter(3)), sum(Iter(3)), max(Iter(3)))


class Ctx:
    def __enter__(self):
        probe("enter")
        return "v"

    def __exit__(self, *a):
        return False


with Ctx() as v:
    print(v)

print(" ".join(str(x) for x in range(3)))
print(any(x > 1 for x in [0, 1, 2]))
print(dict(zip("ab", (x for x in range(2)))))


def raising_key(x):
    raise ValueError("in key")


try:
    sorted([1, 2], key=raising_key)
except ValueError as e:
    print("sorted propagated", e)

try:
    list(map(raising_key, [1]))
except ValueError as e:
    print("map propagated", e)


class Cmp:
    def __init__(self, v):
        self.v = v

    def __lt__(self, o):
        return self.v < o.v


print([c.v for c in sorted([Cmp(2), Cmp(1)])])
print(functools.partial(lambda a, b: a - b, 10)(3))


def deco(fn):
    @functools.wraps(fn)
    def wrapper(*a):
        return fn(*a) + 1
    return wrapper


@deco
def plus(x):
    return x


print(plus(1))
print(str(123).__class__, format(1.5, ".1f"), f"{plus(1):03d}")
print(sorted({"b": 1, "a": 2}.items(), key=lambda kv: kv[1]))
probe("end")
