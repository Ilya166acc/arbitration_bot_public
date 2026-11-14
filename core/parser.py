"""
Обёртка над KAD enricher для использования в нашем боте.
Автоматически обновляет куки при ошибке 451.

Ключевое требование к надёжности: ни один вызов здесь не должен висеть
бесконечно. Playwright (sync API) не умеет прерывать собственные зависшие
операции, поэтому обновление кук вынесено в отдельный дочерний процесс
(multiprocessing.Process) с жёстким тайм-аутом и принудительным
terminate()/kill(). Блокировка _refresh_guard всегда освобождается в блоке
finally основного потока.
"""
import logging
import multiprocessing
from multiprocessing.connection import wait as wait_connections
import os
import platform
import random
import signal
import sys
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

# Путь к папке enricher
ENRICHER_DIR = os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..", "enricher"
))
if ENRICHER_DIR not in sys.path:
    sys.path.insert(0, ENRICHER_DIR)

COOKIES_FILE = os.path.join(ENRICHER_DIR, "cookies.json")

# Жёсткий лимит на обновление кук: дольше ждать бессмысленно,
# дальше начинается зависание бота (инцидент: >10 часов ожидания).
COOKIE_REFRESH_TIMEOUT = float(os.getenv("COOKIE_REFRESH_TIMEOUT", "60"))
PROCESS_TERMINATE_GRACE_SECONDS = 3.0

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

# Гарантирует, что одновременно живёт только одна попытка обновления кук.
# Освобождается в блоке finally основного потока после завершения или
# принудительного убийства дочернего процесса.
_refresh_guard = threading.Lock()


class CookieRefreshError(RuntimeError):
    """Playwright не смог получить куки."""


class CookieRefreshTimeout(TimeoutError):
    """Обновление кук не уложилось в отведённое время."""


def _refresh_worker(
    conn,
    headless: bool,
    offscreen: bool,
    proxy: dict | None = None,
) -> None:
    """Тело дочернего процесса: браузер + сбор кук."""
    # Создаём отдельную группу процессов (POSIX), чтобы при тайм-ауте можно
    # было убить не только воркер, но и дочерние процессы Playwright/Chromium.
    if hasattr(os, "setsid"):
        try:
            os.setsid()
        except OSError:
            pass

    if not logging.getLogger().handlers:
        logging.basicConfig(
            level=os.getenv("LOG_LEVEL", "INFO"),
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        )

    try:
        data = get_cookies_via_browser(
            start_url=START_URL,
            proxy=proxy,
            user_agent=USER_AGENT,
            headless=headless,
            offscreen=offscreen,
        )
        payload = {"ok": True, "data": data}
    except Exception as exc:
        # Передаём сериализуемые поля: исключения Playwright могут быть не-picklable.
        payload = {
            "ok": False,
            "error": str(exc),
            "error_type": type(exc).__name__,
            "is_timeout": (
                isinstance(exc, TimeoutError)
                or "timeout" in type(exc).__name__.lower()
            ),
        }

    try:
        conn.send(payload)
    except Exception:
        # Основной процесс уже мог закрыть канал по тайм-ауту.
        pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _terminate_process(proc) -> None:
    """Гарантированно завершает дочерний процесс (terminate -> kill -> join).

    На POSIX дополнительно завершает всю группу процессов воркера, если его
    пришлось убивать принудительно, чтобы не оставлять осиротевший Chromium.
    """
    if proc is None:
        return

    try:
        alive = proc.is_alive()
    except ValueError:
        # Процесс уже закрыт (proc.close() вызывался ранее).
        return

    pid = getattr(proc, "pid", None)
    pgid = None
    if alive and pid and hasattr(os, "getpgid") and hasattr(os, "killpg"):
        try:
            candidate_pgid = os.getpgid(pid)
            if candidate_pgid == pid and candidate_pgid != os.getpgid(0):
                pgid = candidate_pgid
        except OSError:
            pgid = None

    if alive:
        if pgid is not None:
            try:
                os.killpg(pgid, signal.SIGTERM)
            except OSError:
                pass
        try:
            proc.terminate()
        except OSError:
            pass

        proc.join(timeout=PROCESS_TERMINATE_GRACE_SECONDS)

        if proc.is_alive():
            if pgid is not None:
                try:
                    os.killpg(pgid, signal.SIGKILL)
                except OSError:
                    pass
            try:
                proc.kill()
            except OSError:
                pass
            proc.join(timeout=5)

        # Добиваем возможные оставшиеся процессы Chromium в группе воркера.
        if pgid is not None:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except OSError:
                pass
    elif pid is not None:
        proc.join(timeout=1)

    if not proc.is_alive():
        try:
            proc.close()
        except (AttributeError, ValueError):
            pass


def refresh_cookies(timeout: float | None = None) -> None:
    """Обновляет cookies.json через Playwright в отдельном процессе с жёстким тайм-аутом.

    Работает в дочернем daemon-процессе `multiprocessing.Process`: если
    Playwright зависает дольше `timeout` секунд, процесс принудительно
    убивается через `proc.terminate()` / `proc.kill()`, а `_refresh_guard`
    гарантированно освобождается в блоке `finally` основного потока.
    """
    timeout = COOKIE_REFRESH_TIMEOUT if timeout is None else timeout
    proxy_settings = parse_proxy_url(PROXY_URL)
    proxy = proxy_settings.as_playwright() if proxy_settings else None

    if not _refresh_guard.acquire(blocking=False):
        raise CookieRefreshTimeout(
            "Предыдущая попытка обновления кук всё ещё не завершилась"
        )

    proc = None
    parent_conn = None
    child_conn = None

    try:
        logger.info("🔁 Обновляю куки через Playwright (тайм-аут %.0f с)...", timeout)
        is_windows = platform.system() == "Windows"

        # Используем 'spawn', чтобы избежать дедлоков fork() в многопоточном
        # процессе (asyncio + asyncio.to_thread) на Linux и одинаково работать
        # на Linux и Windows.
        mp_ctx = multiprocessing.get_context("spawn")
        parent_conn, child_conn = mp_ctx.Pipe(duplex=False)
        proc = mp_ctx.Process(
            target=_refresh_worker,
            args=(
                child_conn,
                not is_windows,  # На Linux будет True
                is_windows,      # На Windows будет True
                proxy,
            ),
            name="cookie-refresh",
            daemon=True,
        )
        proc.start()
        # Закрываем пишущий конец канала в родителе, чтобы при аварийном
        # падении воркера сразу получить EOF на parent_conn.
        child_conn.close()
        child_conn = None

        ready = wait_connections([parent_conn, proc.sentinel], timeout=timeout)
        if not ready:
            logger.critical(
                "⛔ Playwright не уложился в %.0f с — принудительно завершаю процесс (pid=%s).",
                timeout,
                proc.pid,
            )
            _terminate_process(proc)
            proc = None
            raise CookieRefreshTimeout(
                f"Обновление кук через Playwright не завершилось за {timeout:.0f} с"
            )

        result = None
        if parent_conn in ready or parent_conn.poll():
            try:
                result = parent_conn.recv()
            except (EOFError, OSError):
                result = None

        # Даём воркеру корректно завершиться после отправки результата.
        proc.join(timeout=5)

        if not isinstance(result, dict):
            exit_code = proc.exitcode
            logger.error(
                "❌ Процесс Playwright завершился без результата (код %s).",
                exit_code,
            )
            raise CookieRefreshError(
                f"Процесс Playwright завершился без результата (код {exit_code})"
            )

        if not result.get("ok"):
            raw_error = result.get("error") or "неизвестная ошибка"
            error_type = result.get("error_type") or "Error"
            is_timeout = bool(result.get("is_timeout"))
            safe_error = (
                redact_proxy_error(raw_error, proxy_settings.url)
                if proxy_settings is not None
                else raw_error
            )

            if is_timeout:
                logger.error("❌ Playwright завершился с тайм-аутом: %s", safe_error)
                raise CookieRefreshError(
                    f"Ошибка Playwright при обновлении кук: {safe_error}"
                )

            if proxy_settings is not None:
                logger.error(
                    "❌ Прокси %s не сработал при обновлении кук: %s",
                    proxy_settings.server,
                    safe_error,
                )
                raise CookieRefreshError(
                    f"Прокси {proxy_settings.server} не сработал при обновлении кук: {safe_error}"
                )

            logger.error(
                "❌ Не удалось получить куки через Playwright (%s): %s",
                error_type,
                safe_error,
            )
            raise CookieRefreshError(
                f"Не удалось получить куки через Playwright ({error_type}): {safe_error}"
            )

        data = result.get("data")
        cookies = (data or {}).get("cookies") or {}
        if not cookies:
            raise CookieRefreshError("Playwright вернул пустой набор кук")

        save_cookies(COOKIES_FILE, data)
        logger.info("✅ Куки обновлены: %d шт.", len(cookies))
    finally:
        for conn in (child_conn, parent_conn):
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
        _terminate_process(proc)
        _refresh_guard.release()


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
