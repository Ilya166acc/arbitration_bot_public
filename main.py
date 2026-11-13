import asyncio
import logging
import os
import subprocess
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramNetworkError
from aiogram.types import ErrorEvent, TelegramObject

from config import ALLOWED_USER_IDS, BOT_TOKEN, LOG_LEVEL
from bot.handlers import router
from database.models import create_tables  # <-- ИМПОРТ

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)


def ensure_playwright_browser():
    """Проверяет наличие Chromium и скачивает при необходимости."""
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser_path = p.chromium.executable_path
            if not os.path.exists(browser_path):
                logger.info("⏳ Chromium не найден. Скачиваю...")
                subprocess.run(
                    ["playwright", "install", "chromium"],
                    check=True,
                    timeout=900,  # установка не должна вешать запуск бота
                )
                logger.info("✅ Chromium установлен!")
    except Exception as e:
        logger.warning(f"⚠️ Ошибка проверки Playwright: {e}")


class AccessMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        user_id = user.id if user else None
        if not ALLOWED_USER_IDS or user_id not in ALLOWED_USER_IDS:
            logger.warning(f"Denied access for user_id={user_id}")
            return
        return await handler(event, data)


async def on_error(event: ErrorEvent) -> bool:
    """Глобальный обработчик ошибок aiogram.

    Сетевые сбои api.telegram.org (SSL reset, тайм-ауты) не должны
    останавливать polling: логируем и считаем обновление обработанным.
    Остальные ошибки оставляем стандартной обработке aiogram.
    """
    if isinstance(event.exception, TelegramNetworkError):
        logger.error(
            "🌐 Сетевая ошибка Telegram, polling продолжается: %s",
            event.exception,
        )
        return True
    return False


async def main():
    if not BOT_TOKEN:
        logger.error("❌ BOT_TOKEN не найден!")
        return

    # 1. Создаем/проверяем таблицы БД
    create_tables()
    logger.info("✅ База данных инициализирована")

    # 2. Запускаем бота
    logger.info("🚀 Запускаю бота...")
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.errors.register(on_error)
    dp.update.outer_middleware(AccessMiddleware())
    dp.include_router(router)

    await dp.start_polling(bot)


if __name__ == "__main__":
    ensure_playwright_browser()
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("🛑 Бот остановлен.")