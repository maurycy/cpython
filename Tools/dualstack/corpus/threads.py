import sys
import threading

sys.setrecursionlimit(200)
LOCK = threading.Lock()
LOG = []


def rec(n):
    try:
        return rec(n + 1)
    except RecursionError:
        return n


def gen():
    yield 1
    yield 2


def worker(i):
    with LOCK:
        probe(f"worker-{i}")
    g = gen()
    depth = rec(0)
    with LOCK:
        LOG.append((i, depth, list(g)))


threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
for t in threads:
    t.start()
for t in threads:
    t.join()
print(sorted(LOG))

event = threading.Event()
done = threading.Event()


def waiter():
    event.wait()
    done.set()


t = threading.Thread(target=waiter, name="waiter")
t.start()
frames = sys._current_frames()
print("waiter visible", any(f.f_code.co_name == "waiter" for f in frames.values()))
event.set()
done.wait()
t.join()


def in_thread_exception():
    raise ValueError("thread exc")


res = []
orig = threading.excepthook
threading.excepthook = lambda args: res.append(args.exc_type.__name__)
t = threading.Thread(target=in_thread_exception)
t.start()
t.join()
threading.excepthook = orig
print("excepthook", res)
print("main depth", rec(0))
probe("end")
