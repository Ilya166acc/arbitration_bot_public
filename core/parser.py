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

from config import PROXY_URL
from core.proxy import parse_proxy_url, redact_proxy_error
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


def _refresh_worker(headless: bool, offscreen: bool, proxy: dict | None = None) -> dict:
    """Тело фонового потока: браузер + сбор кук."""
    try:
        return get_cookies_via_browser(
            start_url=START_URL,
            proxy=proxy,
            user_agent=USER_AGENT,
            headless=headless,
            offscreen=offscreen,
        )
    finally:
        # Флаг занятости снимает именно поток-исполнитель: если его abandon-ят
        # по тайм-ауту, новые попытки не будут наслаиваться.
        _refresh_guard.release()


def refresh_cookies(timeout: float | None = None) -> None:
    """Обновляет cookies.json через Playwright с жёстким тайм-аутом.

    Работает в отдельном потоке: `future.result(timeout=...)` не даёт боту
    провисеть дольше `timeout` секунд. По истечении поднимается
    CookieRefreshTimeout (наследник TimeoutError).
    """
    timeout = COOKIE_REFRESH_TIMEOUT if timeout is None else timeout
    proxy_settings = parse_proxy_url(PROXY_URL)
    proxy = proxy_settings.as_playwright() if proxy_settings else None

    if not _refresh_guard.acquire(blocking=False):
        raise CookieRefreshTimeout(
            "Предыдущая попытка обновления кук всё ещё не завершилась"
        )

    logger.info("🔁 Обновляю куки через Playwright (тайм-аут %.0f с)...", timeout)
    is_windows = platform.system() == "Windows"

    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cookie-refresh")
    try:
        future = executor.submit(
            _refresh_worker,
            headless=not is_windows,   # На Linux будет True
            offscreen=is_windows,      # На Windows будет True
            proxy=proxy,
        )
    except BaseException:
        # Задача не встала в очередь — поток флаг не снимет, делаем это сами.
        _refresh_guard.release()
        executor.shutdown(wait=False)
        raise

    try:
        data = future.result(timeout=timeout)
    except (TimeoutError, concurrent.futures.TimeoutError) as exc:
        if not future.done():
            # Жёсткий тайм-аут: поток всё ещё работает (зависший браузер).
            future.cancel()
            logger.critical(
                "⛔ Playwright не уложился в %.0f с — прерываю обновление кук.",
                timeout,
            )
            raise CookieRefreshTimeout(
                f"Обновление кук через Playwright не завершилось за {timeout:.0f} с"
            ) from exc
        # Поток завершился, но внутри Playwright случился собственный TimeoutError.
        logger.error("❌ Playwright завершился с тайм-аутом: %s", exc)
        raise CookieRefreshError(f"Ошибка Playwright при обновлении кук: {exc}") from exc
    except Exception as exc:
        if proxy_settings is not None:
            safe_error = redact_proxy_error(exc, proxy_settings.url)
            logger.error(
                "❌ Прокси %s не сработал при обновлении кук: %s",
                proxy_settings.server,
                safe_error,
            )
            raise CookieRefreshError(
                f"Прокси {proxy_settings.server} не сработал при обновлении кук: {safe_error}"
            ) from exc
        logger.exception("❌ Не удалось получить куки через Playwright")
        raise
    else:
        cookies = (data or {}).get("cookies") or {}
        if not cookies:
            raise CookieRefreshError("Playwright вернул пустой набор кук")
        save_cookies(COOKIES_FILE, data)
        logger.info("✅ Куки обновлены: %d шт.", len(cookies))
    finally:
        # wait=False: поток с зависшим браузером не должен блокировать
        # вызывающий код. cancel_futures намеренно не используется —
        # задача должна выполниться, иначе _refresh_guard останется занятым.
        executor.shutdown(wait=False)


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
