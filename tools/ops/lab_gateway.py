"""Bounded TCP relay to one fixed isolated lab application, never an open proxy."""
import asyncio

MAX_CLIENTS = 16
CLIENTS = set()


async def pipe(reader, writer):
    while data := await reader.read(65536):
        writer.write(data)
        await writer.drain()


async def relay(reader, writer):
    task = asyncio.current_task()
    if len(CLIENTS) >= MAX_CLIENTS:
        writer.close()
        return
    CLIENTS.add(task)
    upstream = None
    jobs = []
    try:
        remote, upstream = await asyncio.wait_for(asyncio.open_connection('linkding', 9090), 5)
        jobs = [asyncio.create_task(pipe(reader, upstream)), asyncio.create_task(pipe(remote, writer))]
        done, _ = await asyncio.wait(jobs, timeout=180, return_when=asyncio.FIRST_COMPLETED)
        for completed in done:
            completed.result()
    except (OSError, asyncio.TimeoutError, ConnectionError):
        pass
    finally:
        for job in jobs:
            job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
        for connection in (writer, upstream):
            if connection:
                connection.close()
        CLIENTS.discard(task)


async def main():
    server = await asyncio.start_server(relay, '0.0.0.0', 8080, limit=65536)
    async with server:
        await server.serve_forever()


if __name__ == '__main__':
    asyncio.run(main())
