import sys
import traceback


def raiser(kind):
    raise kind("from raiser")


def level2(kind):
    raiser(kind)


def level1(kind):
    try:
        level2(kind)
    finally:
        print("finally in level1")


def tb_names(exc):
    return [(f.name, f.lineno - LINE0) for f in traceback.extract_tb(exc.__traceback__)]


LINE0 = 0

try:
    level1(ValueError)
except ValueError as e:
    print("tb", tb_names(e))
    probe("in-handler")

try:
    try:
        raiser(KeyError)
    except KeyError as e:
        raise TypeError("chained") from e
except TypeError as e:
    print("cause", type(e.__cause__).__name__, "context", type(e.__context__).__name__)

try:
    try:
        raiser(KeyError)
    finally:
        print("cleanup sees", sys.exception().__class__.__name__)
except KeyError:
    print("key after finally")


def reraise():
    try:
        raiser(OSError)
    except OSError:
        raise


try:
    reraise()
except OSError as e:
    print("reraised", tb_names(e))


class CM:
    def __enter__(self):
        return self

    def __exit__(self, t, v, tb):
        print("exit sees", t.__name__ if t else None)
        return t is ZeroDivisionError


with CM():
    1 / 0
print("suppressed")

try:
    with CM():
        raiser(IndexError)
except IndexError:
    print("not suppressed")


def nested_handlers():
    try:
        raiser(ValueError)
    except ValueError:
        try:
            raiser(KeyError)
        except KeyError:
            print("inner handled, outer is", sys.exception().__class__.__name__)
        print("back to outer", sys.exception().__class__.__name__)
    print("after all", sys.exception())


nested_handlers()

try:
    raise ExceptionGroup("eg", [ValueError(1), TypeError(2)])
except* ValueError as eg:
    print("star value", eg.exceptions)
except* TypeError as eg:
    print("star type", eg.exceptions)


def gen_raises():
    yield 1
    raiser(RuntimeError)


try:
    list(gen_raises())
except RuntimeError as e:
    print("gen tb", tb_names(e))


def deep(n):
    if n == 0:
        raiser(LookupError)
    deep(n - 1)


try:
    deep(20)
except LookupError as e:
    print("deep tb frames", len(traceback.extract_tb(e.__traceback__)))

try:
    sys.exit(3)
except SystemExit as e:
    print("exit code", e.code)

probe("end")
