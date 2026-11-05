import asyncio
import logging

from aiohttp import web

from .api import build_app
from .config import BOT_TOKEN, RUN_BOT, PORT, WEBAPP_URL
from .db import init_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("main")


async def main():
    await init_db()
    bot = None
    if RUN_BOT and BOT_TOKEN:
        from .bot import make_bot
        bot = make_bot()
    runner = web.AppRunner(build_app(bot))
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    log.info("web on :%s, webapp url: %s", PORT, WEBAPP_URL or "(не задан)")
    if bot:
        from .bot import setup_bot, make_dispatcher
        try:
            await setup_bot(bot)
        except Exception as e:  # noqa
            log.warning("setup_bot: %s", e)
        await bot.delete_webhook(drop_pending_updates=False)
        await make_dispatcher().start_polling(bot)
    else:
        log.info("бот не запущен (RUN_BOT=0 или нет BOT_TOKEN) — работает только сервер")
        await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
