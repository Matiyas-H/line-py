from main import create_application, run_application
import asyncio

async def app(scope, receive, send):
    if scope['type'] == 'lifespan':
        while True:
            message = await receive()
            if message['type'] == 'lifespan.startup':
                # Initialize your application here
                await run_application()
                await send({'type': 'lifespan.startup.complete'})
            elif message['type'] == 'lifespan.shutdown':
                # Perform cleanup here if needed
                await send({'type': 'lifespan.shutdown.complete'})
                return
    elif scope['type'] == 'websocket':
        task, runner, transport = await create_application()
        await runner.run(task)
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