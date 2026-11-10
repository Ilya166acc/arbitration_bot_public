import asyncio
import logging
import os
import subprocess

from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties

from config import BOT_TOKEN, LOG_LEVEL
from bot.handlers import router
from database.models import create_tables  # <-- ИМПОРТ ДЛЯ БАЗЫ ДАННЫХ


# Настраиваем логирование
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)


def ensure_playwright_browser():
    """
    Проверяет, установлен ли браузер Chromium для Playwright.
    Если файла нет — скачивает его.
    """
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser_path = p.chromium.executable_path
            if not os.path.exists(browser_path):
                logger.info("⏳ Обнаружено отсутствие Chromium. Скачиваю браузер (это займет пару минут)...")
                subprocess.run(["playwright", "install", "chromium"], check=True)
                logger.info("✅ Chromium успешно установлен!")
    except Exception as e:
        logger.warning(f"⚠️ Предупреждение при проверке Playwright: {e}")


async def main():
    """Главная функция — запускает бота."""
    
    # Проверяем, что токен вообще есть
    if not BOT_TOKEN:
        logger.error("❌ BOT_TOKEN не найден! Проверь переменные окружения")
        return
    
    # 1. ИНИЦИАЛИЗИРУЕМ БАЗУ ДАННЫХ (создаем таблицы, если их нет)
    create_tables()
    logger.info("✅ База данных инициализирована")
    
    logger.info("🚀 Запускаю бота...")
    
    # Создаём бота
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )
    
    # Создаём диспетчер
    dp = Dispatcher()
    
    # Подключаем роутер с командами
    dp.include_router(router)
    
    # Запускаем бота
    logger.info("✅ Бот запущен. Нажми Ctrl+C для остановки.")
    await dp.start_polling(bot)


if __name__ == "__main__":
    # Проверяем браузер ДО запуска бота
    ensure_playwright_browser()

    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("🛑 Бот остановлен.")