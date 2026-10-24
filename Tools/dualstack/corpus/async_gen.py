import asyncio


async def agen(n):
    try:
        for i in range(n):
            got = yield i
            if got:
                print("agen got", got)
    finally:
        print("agen finally")


async def agen_raises():
    yield 1
    raise ValueError("agen")


async def consume():
    out = []
    async for x in agen(3):
        probe("async-for")
        out.append(x)
    print("consumed", out)

    g = agen(3)
    print(await g.asend(None), await g.asend("v"))
    await g.aclose()

    g = agen(2)
    await g.__anext__()
    try:
        await g.athrow(KeyError("thrown"))
    except KeyError as e:
        print("athrow propagated", e)

    try:
        async for x in agen_raises():
            print("got", x)
    except ValueError as e:
        print("caught", e)

    print([x async for x in agen(2)])
    print({x: 1 async for x in agen(2)})

    g = agen(5)
    await g.__anext__()
    print("ag_frame", g.ag_frame.f_lineno - agen.__code__.co_firstlineno, g.ag_running, g.ag_suspended)
    probe("suspended-agen")
    del g
    await asyncio.sleep(0)


asyncio.run(consume())
probe("end")
