import sys

EVENTS = []


def tracer(frame, event, arg):
    if frame.f_code.co_filename == __file__:
        EVENTS.append((event, frame.f_code.co_name, frame.f_lineno - frame.f_code.co_firstlineno,
                       type(arg).__name__ if event == "exception" else arg if event == "return" else None))
    return tracer


def profiler(frame, event, arg):
    if frame.f_code.co_filename == __file__:
        name = arg.__name__ if event.startswith("c_") else frame.f_code.co_name
        EVENTS.append(("p:" + event, name))


def gen():
    yield 1
    yield 2


def raises():
    raise KeyError("x")


def work():
    for x in gen():
        pass
    try:
        raises()
    except KeyError:
        pass
    len([1])
    probe("traced")
    return 5


sys.settrace(tracer)
work()
sys.settrace(None)
print(len(EVENTS))
for e in EVENTS:
    print(e)

EVENTS.clear()
sys.setprofile(profiler)
work()
sys.setprofile(None)
for e in EVENTS:
    print(e)


def jump_target():
    x = 1
    x = 2
    x = 3
    return x


def jumper(frame, event, arg):
    if event == "line" and frame.f_code is jump_target.__code__ and frame.f_lineno == jump_target.__code__.co_firstlineno + 2:
        frame.f_lineno = jump_target.__code__.co_firstlineno + 4
    return jumper


sys.settrace(jumper)
print("jumped", jump_target())
sys.settrace(None)


def local_trace_only():
    f = sys._getframe()
    f.f_trace = tracer
    f.f_trace_lines = False
    f.f_trace_opcodes = False
    return "local"


EVENTS.clear()
sys.settrace(tracer)
local_trace_only()
sys.settrace(None)
print("local events", [e[0] for e in EVENTS])
probe("end")
