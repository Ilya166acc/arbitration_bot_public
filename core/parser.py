"""
Обёртка над KAD enricher для использования в нашем боте.
Автоматически обновляет куки при ошибке 451.
"""
import sys
import os
import logging
import platform
import time
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# Путь к папке enricher
ENRICHER_DIR = os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..", "enricher"
))
sys.path.insert(0, ENRICHER_DIR)

COOKIES_FILE = os.path.join(ENRICHER_DIR, "cookies.json")

from services.cases import get_cases, CookieExpiredError  # type: ignore
from services.cookies import get_cookies_via_browser, save_cookies  # type: ignore


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
START_URL = "https://kad.arbitr.ru/"


def refresh_cookies() -> None:
    logger.info("🔁 Обновляю куки через Playwright...")
    is_windows = platform.system() == "Windows"
    
    data = get_cookies_via_browser(
        start_url=START_URL,
        proxy=None,
        user_agent=USER_AGENT,
        headless=not is_windows,   # На Linux будет True
        offscreen=is_windows,      # На Windows будет True
    )
    save_cookies(COOKIES_FILE, data)
    logger.info(f"✅ Куки обновлены: {len(data.get('cookies', {}))} шт.")


def fetch_cases_by_inn(inn: str, days_back: int = 30, cookies_file: str = COOKIES_FILE) -> list:
    # Вычисляем даты
    date_to = datetime.now().strftime("%d.%m.%Y")
    date_from = (datetime.now() - timedelta(days=days_back)).strftime("%d.%m.%Y")
    
    logger.info(f"🔍 Ищем дела по ИНН {inn} с {date_from} по {date_to}")

    def _fetch_all_pages() -> list:
        all_cases = []
        page = 1
        while True:
            rows, _, _, _, pages_count = get_cases(
                cookies_file=cookies_file,
                page=page,
                count=25,
                courts=None,
                sides=[inn],
                date_from=date_from,
                date_to=date_to
            )
            all_cases.extend(rows)
            if page >= pages_count:
                break
            page += 1
        return all_cases

    # Пытаемся выполнить запрос до 2 раз
    for attempt in range(2):
        try:
            return _fetch_all_pages()
        except (CookieExpiredError, FileNotFoundError):
            if attempt == 0:
                logger.warning("⚠️ Куки протухли или отсутствуют, обновляю и повторяю попытку...")
                refresh_cookies()
                time.sleep(3)  # <--- Даем сайту 3 секунды, чтобы "принять" новые куки
            else:
                logger.error("❌ Не удалось обновить куки со второй попытки.")
                raise  # Если вторая попытка провалилась, выбрасываем ошибку наверх