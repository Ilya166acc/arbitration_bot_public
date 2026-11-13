"""
Обёртка над KAD enricher для использования в нашем боте.
Автоматически обновляет куки при ошибке 451.

Ключевое требование к надёжности: ни один вызов здесь не должен висеть
бесконечно. Playwright (sync API) не умеет прерывать собственные зависшие
операции, поэтому обновление кук вынесено в отдельный поток и ограничено
жёстким тайм-аутом.
"""
import concurrent.futures
import logging
import multiprocessing as mp
import os
import platform
import random
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

# Путь к папке enricher
ENRICHER_DIR = os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..", "enricher"
))
sys.path.insert(0, ENRICHER_DIR)

COOKIES_FILE = os.path.join(ENRICHER_DIR, "cookies.json")

# Жёсткий лимит на обновление кук: дольше ждать бессмысленно,
# дальше начинается зависание бота (инцидент: >10 часов ожидания).
COOKIE_REFRESH_TIMEOUT = float(os.getenv("COOKIE_REFRESH_TIMEOUT", "60"))

from services.cases import get_cases, CookieExpiredError  # type: ignore
from services.cookies import get_cookies_via_browser, save_cookies  # type: ignore


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
START_URL = "https://kad.arbitr.ru/"

# Гарантирует, что одновременно живёт только одна попытка обновления кук:
# зависший поток Playwright не должен плодить новые браузеры.
_refresh_guard = threading.Lock()


class CookieRefreshError(RuntimeError):
    """Playwright не смог получить куки."""


class CookieRefreshTimeout(TimeoutError):
    """Обновление кук не уложилось в отведённое время."""



def _refresh_entry(headless, offscreen, queue):
    try:
        data = get_cookies_via_browser(START_URL, None, USER_AGENT, headless=headless, offscreen=offscreen)
        queue.put(("ok", data))
    except BaseException as e:
        queue.put(("err", repr(e)))


def refresh_cookies() -> None:
    logger.info("Обновляю куки через Playwright (процесс)...")
    if not _refresh_guard.acquire(blocking=False):
        raise CookieRefreshTimeout("Previous refresh still running")

    queue = mp.Queue()
    is_win = platform.system() == "Windows"
    proc = mp.Process(target=_refresh_entry, args=(not is_win, is_win, queue), daemon=True)
    proc.start()

    try:
        status, payload = queue.get(timeout=COOKIE_REFRESH_TIMEOUT)
    except Exception:
        logger.error("Процесс обновления куки завис. Убиваю.")
        proc.terminate()
        proc.join(5)
        if proc.is_alive():
            proc.kill()
            proc.join()
        raise CookieRefreshTimeout("Refresh timed out")
    finally:
        _refresh_guard.release()
        proc.join(0)

    if status == "err":
        raise CookieRefreshError(payload)
    if not payload.get("cookies"):
        raise CookieRefreshError("empty cookie set")

    save_cookies(COOKIES_FILE, payload)
    logger.info(f"Куки обновлены: {len(payload.get('cookies', {}))} шт.")


def fetch_cases_by_inn(inn: str, days_back: int = 30, cookies_file: str = COOKIES_FILE,
                       max_pages: int = 50) -> list:
    now = datetime.now(ZoneInfo("Europe/Moscow"))
    date_to = now.strftime("%d.%m.%Y")
    date_from = (now - timedelta(days=days_back)).strftime("%d.%m.%Y")
    logger.info(f"🔍 Ищем дела по ИНН {inn} с {date_from} по {date_to}")

    def _fetch_all_pages() -> list:
        all_cases = []
        page = 1
        while True:
            rows, _, _, _, pages_count, html = get_cases(
                cookies_file=cookies_file, page=page, count=25,
                courts=None, sides=[inn],
                date_from=date_from, date_to=date_to, include_html=True
            )
            if not rows and "num_case" not in html and "Дела не найдены" not in html:
                raise CookieExpiredError("Вместо результатов поиска получена пустая страница или проверка DDoS-Guard")
            all_cases.extend(rows)

            try:
                pages_count = int(pages_count)
            except (TypeError, ValueError):
                pages_count = 1

            if page >= pages_count:
                break
            # Защита от бесконечного цикла, если сайт вернул некорректное
            # количество страниц (например, 999999). max_pages ограничивает
            # и трафик, и время проверки одного ИНН.
            if page >= max_pages:
                logger.warning(
                    "⚠️ Достигнут предел %d страниц для ИНН %s — пагинация остановлена.",
                    max_pages, inn,
                )
                break
            time.sleep(random.uniform(2.0, 4.0))
            page += 1
        return all_cases

    last_error: Exception | None = None
    for attempt in range(2):
        try:
            return _fetch_all_pages()
        except (CookieExpiredError, FileNotFoundError) as exc:
            last_error = exc
            if attempt == 0:
                logger.warning("⚠️ Куки протухли, обновляю...")
                # CookieRefreshTimeout (TimeoutError) пробрасывается наверх:
                # повторять запрос с неработающими куками бессмысленно.
                refresh_cookies()
                time.sleep(3)
            else:
                raise

    # Недостижимо, но без этого читатель может решить, что возможен return None.
    raise last_error or CookieExpiredError("Не удалось получить дела")
