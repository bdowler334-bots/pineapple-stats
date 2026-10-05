import asyncio
import logging
import os
from urllib.parse import urlparse
import uvicorn
from dotenv import load_dotenv
from store import Store
from bot import StatsBot
from server import create_app

load_dotenv()
logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(name)s: %(message)s')
# Avoid HTTP request logs exposing provider API keys in query strings.
logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('httpcore').setLevel(logging.WARNING)

async def main():
    demo=os.getenv('DEMO_MODE','false').lower()=='true'
    if demo and urlparse(os.getenv('PUBLIC_URL','http://localhost:8000')).hostname not in ('localhost','127.0.0.1','::1'):
        raise RuntimeError('Demo mode is restricted to a local PUBLIC_URL')
    store=Store(os.getenv('DATABASE_PATH','./data/pineapple-stats.sqlite3'))
    bot=None if demo else StatsBot(store)
    if demo:
        from demo import seed
        seed(store)
    elif not all(os.getenv(k) and not os.getenv(k).startswith('replace-') for k in ('DISCORD_TOKEN','DISCORD_CLIENT_ID','DISCORD_CLIENT_SECRET')):
        raise RuntimeError('Copy .env.example to .env and set the Discord bot token, application ID, and OAuth2 secret. See START-HERE.md.')
    app=create_app(store,bot,demo)
    host='127.0.0.1' if demo else '0.0.0.0'
    server=uvicorn.Server(uvicorn.Config(app,host=host,port=int(os.getenv('PORT','8000')),proxy_headers=False))
    try:
        if demo:
            await server.serve()
        else:
            bot_task=asyncio.create_task(bot.start(os.environ['DISCORD_TOKEN']))
            web_task=asyncio.create_task(server.serve())
            done,pending=await asyncio.wait((bot_task,web_task),return_when=asyncio.FIRST_COMPLETED)
            server.should_exit=True
            for task in done:
                task.result()
            if bot:
                await bot.close()
            await asyncio.gather(*pending,return_exceptions=True)
    finally:
        if bot:
            bot.flush()
            bot.tick.cancel()
            await bot.close()
        store.close()

if __name__=='__main__':
    asyncio.run(main())
