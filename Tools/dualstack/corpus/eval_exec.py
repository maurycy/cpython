import sys
import types


def f(n):
    probe("f")
    if n == 0:
        return "bottom"
    return eval("f(n - 1)")


print(f(3))

ns = {}
exec("def g():\n    probe('g')\n    return 7\nresult = g()", {"probe": probe}, ns)
print(ns["result"])


def with_locals():
    x = 1
    exec("x = 2; y = 3")
    return x, sorted(k for k in locals() if k in ("x", "y"))


print(with_locals())

code = compile("a + b", "<expr>", "eval")
print(eval(code, {"a": 1, "b": 2}))

mod = types.ModuleType("dyn")
exec("import sys\nvalue = sys._getframe().f_code.co_name", mod.__dict__)
print(mod.value)


def nested_compile():
    src = "def inner():\n    return sys._getframe().f_back.f_code.co_name\nout = inner()"
    d = {"sys": sys}
    exec(src, d)
    return d["out"]


print(nested_compile())

try:
    exec("raise KeyError('x')")
except KeyError as e:
    print("exec raised", e)

print(eval("[i for i in range(3)]"))
print(eval("(lambda: sys._getframe().f_back.f_code.co_name)()", {"sys": sys}))
import importlib  # noqa: E402

m = importlib.import_module("json")
print(m.dumps([1]))
probe("end")
