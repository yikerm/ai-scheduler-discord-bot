"""專案進入點：在同一個 asyncio event loop 啟動 Discord Bot 與 APScheduler。"""

import asyncio
import logging

from bot import bot
from config import DISCORD_BOT_TOKEN
from scheduler import create_scheduler


logging.basicConfig(level=logging.INFO)


async def main() -> None:
    if not DISCORD_BOT_TOKEN:
        raise RuntimeError("缺少 DISCORD_BOT_TOKEN，請在 .env 中設定。")

    scheduler = create_scheduler()
    scheduler.start()
    try:
        async with bot:
            await bot.start(DISCORD_BOT_TOKEN)
    finally:
        if scheduler.running:
            scheduler.shutdown(wait=False)


if __name__ == "__main__":
    asyncio.run(main())
