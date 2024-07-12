import asyncio
import os
from main import create_application

async def proxy_websocket(scope, receive, send):
    task, runner, transport = await create_application()
    await transport.handle_websocket(scope, receive, send)

async def app(scope, receive, send):
    if scope['type'] == 'lifespan':
        while True:
            message = await receive()
            if message['type'] == 'lifespan.startup':
                # Start the WebSocket server on port 8765
                task, runner, transport = await create_application()
                loop = asyncio.get_event_loop()
                loop.create_task(runner.run(task))
                await send({'type': 'lifespan.startup.complete'})
            elif message['type'] == 'lifespan.shutdown':
                await send({'type': 'lifespan.shutdown.complete'})
                return
    elif scope['type'] == 'websocket':
        await proxy_websocket(scope, receive, send)
    else:
        await send({
            'type': 'http.response.start',
            'status': 404,
            'headers': [(b'content-type', b'text/plain')],
        })
        await send({
            'type': 'http.response.body',
            'body': b'Not found',
        })