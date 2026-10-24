class Base:
    def __init__(self, v):
        probe("Base.__init__")
        self.v = v


class Child(Base):
    def __init__(self, v):
        super().__init__(v * 2)
        self.c = True


class BadReturn:
    def __init__(self):
        return 1


class Raises:
    def __init__(self):
        raise ValueError("init failed")


class Meta(type):
    def __call__(cls, *a):
        print("meta call", cls.__name__)
        return super().__call__(*a)


class WithMeta(metaclass=Meta):
    def __init__(self, x):
        self.x = x


class Props:
    def __init__(self):
        self._p = 1

    @property
    def p(self):
        probe("getter")
        return self._p

    @p.setter
    def p(self, v):
        self._p = v

    def __getattr__(self, name):
        return f"dyn-{name}"

    def __len__(self):
        return 3

    def __bool__(self):
        return False

    def __index__(self):
        return 2

    def __iter__(self):
        return iter([1, 2])

    def __eq__(self, other):
        return True

    def __hash__(self):
        return 1

    def __lt__(self, other):
        return True


class NewOnly:
    def __new__(cls, v):
        obj = super().__new__(cls)
        obj.v = v
        return obj


print(Child(2).v, Child(2).c)
try:
    BadReturn()
except TypeError as e:
    print("bad return:", str(e)[:40])
try:
    Raises()
except ValueError as e:
    print("raised:", e)
print(WithMeta(1).x)
p = Props()
print(p.p, p.missing, len(p), bool(p), [1, 2, 3][p], list(p), p == 1, hash(p), p < 2)
p.p = 5
print(p.p)
print(sorted([Props(), Props()])[0] is not None)
print(NewOnly(3).v)
print({p: 1}[Props()])
probe("end")
