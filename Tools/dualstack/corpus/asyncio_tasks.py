import asyncio


async def worker(name, q, results):
    while True:
        item = await q.get()
        if item is None:
            return
        probe(f"worker-{name}")
        results.append((name, item))
        q.task_done()


async def failing():
    await asyncio.sleep(0)
    raise RuntimeError("task failed")


async def cancellable():
    try:
        await asyncio.sleep(10)
    except asyncio.CancelledError:
        print("cancelled cleanly")
        raise


async def nested(n):
    if n == 0:
        stack = asyncio.current_task().get_stack()
        print("task stack", [f.f_code.co_name for f in stack])
        probe("nested-bottom")
        return 0
    return 1 + await nested(n - 1)


async def main():
    q = asyncio.Queue()
    results = []
    workers = [asyncio.create_task(worker(i, q, results), name=f"w{i}") for i in range(2)]
    for i in range(4):
        await q.put(i)
    await q.join()
    for _ in workers:
        await q.put(None)
    await asyncio.gather(*workers)
    print("results", sorted(results))

    t = asyncio.create_task(failing())
    try:
        await t
    except RuntimeError as e:
        print("gathered error", e)

    t = asyncio.create_task(cancellable())
    await asyncio.sleep(0)
    t.cancel()
    try:
        await t
    except asyncio.CancelledError:
        print("await cancelled task")

    print("nested", await nested(3))

    r = await asyncio.gather(nested(1), nested(2), return_exceptions=True)
    print("gather", r)

    async with asyncio.TaskGroup() as tg:
        tg.create_task(nested(1))
        tg.create_task(nested(2))
    print("taskgroup done")

    try:
        async with asyncio.timeout(0.01):
            await asyncio.sleep(1)
    except TimeoutError:
        print("timeout")
    probe("main-end")


asyncio.run(main())
probe("end")
