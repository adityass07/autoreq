import asyncio
# Python 3.14 compatibility: always create and set event loop before pyrogram imports
_loop = asyncio.new_event_loop()
asyncio.set_event_loop(_loop)

import sys
import ctypes
import logging
from bot import bot, dp
import database
import client_manager
from config import BOT_TOKEN

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("MainRunner")

def prevent_windows_sleep():
    """Tells Windows OS to NEVER enter sleep mode while this script is running."""
    try:
        if sys.platform == "win32":
            ES_CONTINUOUS = 0x80000000
            ES_SYSTEM_REQUIRED = 0x00000001
            ES_AWAYMODE_REQUIRED = 0x00000040
            ctypes.windll.kernel32.SetThreadExecutionState(
                ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_AWAYMODE_REQUIRED
            )
            logger.info("☕ Windows Sleep Prevention Active: PC will NOT sleep while Bot is running!")
    except Exception as e:
        logger.warning(f"Could not set Windows execution state: {e}")

async def start_services():
    prevent_windows_sleep()

    if not BOT_TOKEN or BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        logger.error("❌ BOT_TOKEN set nahi hai! Kripya config.py me apna Bot Token daalein.")
        return

    logger.info("📦 Initializing database...")
    await database.init_db()

    logger.info("🤖 Starting Master Telegram Bot (Aiogram)...")
    me = await bot.get_me()
    logger.info(f"✨ Master Bot @{me.username} is now ONLINE & READY to receive messages!")

    logger.info("👥 Loading and starting active user sessions...")
    await client_manager.init_all_clients()

    # Drop pending updates and start polling
    await bot.delete_webhook(drop_pending_updates=True)
    
    logger.info("🟢 Bot is polling now. Send /start on Telegram!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    prevent_windows_sleep()
    while True:
        try:
            asyncio.run(start_services())
        except (KeyboardInterrupt, SystemExit):
            logger.info("👋 Bot stopped manually by user.")
            break
        except Exception as e:
            logger.error(f"⚠️ Bot crashed or network dropped with error: {e}. Auto-restarting in 5 seconds...", exc_info=True)
            import time
            time.sleep(5)
