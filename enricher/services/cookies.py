import json
import logging
import os
from threading import Lock

from playwright.sync_api import sync_playwright

logger = logging.getLogger(__name__)

# === Жёсткие тайм-ауты Playwright (миллисекунды) ===
# Без них page.goto()/click()/is_visible() могут ждать ответа бесконечно:
# именно так проверка зависла более чем на 10 часов.
PAGE_TIMEOUT_MS = int(os.getenv("PLAYWRIGHT_PAGE_TIMEOUT_MS", "30000"))
BROWSER_LAUNCH_TIMEOUT_MS = int(os.getenv("PLAYWRIGHT_LAUNCH_TIMEOUT_MS", "30000"))

# Внутренний бюджет ожиданий (~45 c), чтобы гарантированно уложиться
# в 60-секундный жёсткий тайм-аут из core/parser.py.
NAVIGATION_SETTLE_MS = 3000
ACTION_SETTLE_MS = 500
CLICK_RESULT_WAIT_MS = 4000
VISIBILITY_TIMEOUT_MS = 2000

_cookie_write_lock = Lock()


def get_cookies_via_browser(start_url: str, proxy: dict | None, user_agent: str,
                            headless: bool = False, offscreen: bool = False) -> dict:
    """Открывает сайт в Chromium и собирает куки (обход DDoS-Guard).

    Все операции ограничены PAGE_TIMEOUT_MS. Браузер и контекст закрываются
    в finally, даже если внутри произошла ошибка или тайм-аут.
    """
    with sync_playwright() as p:
        args = ['--disable-blink-features=AutomationControlled']
        if headless:
            args.append('--headless=new')
        elif offscreen:
            args.extend(['--window-position=-32000,-32000', '--window-size=1366,768'])

        launch_kwargs = {
            "headless": False,
            "args": args,
            # Браузер не должен запускаться дольше, чем длится вся проверка.
            "timeout": BROWSER_LAUNCH_TIMEOUT_MS,
        }
        if proxy:
            launch_kwargs["proxy"] = proxy

        logger.info("Запускаю браузер для получения кук...")
        browser = p.chromium.launch(**launch_kwargs)
        context = None

        try:
            context = browser.new_context(
                user_agent=user_agent,
                locale='ru-RU',
                timezone_id='Europe/Moscow',
                ignore_https_errors=True,
                viewport={"width": 1366, "height": 768},
            )
            # Глобальные тайм-ауты: любой вызов без явного timeout
            # ограничен 30 секундами вместо бесконечного ожидания.
            context.set_default_timeout(PAGE_TIMEOUT_MS)
            page = context.new_page()
            page.set_default_timeout(PAGE_TIMEOUT_MS)

            logger.info("Перехожу на %s", start_url)
            try:
                response = page.goto(start_url, wait_until='domcontentloaded', timeout=PAGE_TIMEOUT_MS)
                if proxy and response is not None and response.status == 407:
                    raise RuntimeError("Proxy authentication required (HTTP 407)")
                page.wait_for_timeout(NAVIGATION_SETTLE_MS)
            except Exception as e:
                if proxy:
                    # С настроенным прокси нельзя молча продолжать с пустыми
                    # куками: ошибка авторизации/туннеля должна дойти до бота.
                    logger.error("Не удалось загрузить страницу через прокси: %s", e)
                    raise
                logger.warning("Не удалось загрузить страницу: %s", e)

            # === Имитация активности человека (обход антибота) ===
            try:
                page.mouse.move(500, 300)
                page.mouse.click(500, 300)
                page.wait_for_timeout(ACTION_SETTLE_MS)

                page.mouse.wheel(0, 500)
                page.wait_for_timeout(ACTION_SETTLE_MS)

                find_btn = page.locator('button[type="submit"]', has_text="Найти")
                if find_btn.first.is_visible(timeout=VISIBILITY_TIMEOUT_MS):
                    find_btn.first.click()
                    page.wait_for_timeout(CLICK_RESULT_WAIT_MS)
            except Exception as e:
                logger.warning("Ошибка при имитации кликов: %s", e)
            # =====================================================

            logger.info("Собираю куки...")
            cookie_list = context.cookies()
            cookie_dict = {c['name']: c['value'] for c in cookie_list}
            logger.info("Собрано кук: %d", len(cookie_dict))
            return {"cookies": cookie_dict, "cookiesRaw": cookie_list}

        except Exception as e:
            logger.error("Критическая ошибка внутри Playwright: %s", e)
            if proxy:
                # Ошибки прокси пробрасываем, а не маскируем пустым набором кук.
                raise
            return {"cookies": {}, "cookiesRaw": []}
        finally:
            # Гарантированно освобождаем ресурсы, чтобы Chromium не висел в памяти.
            # Закрытие тоже может бросить исключение — это не должно маскировать
            # исходную ошибку и не должно оставлять процесс браузера.
            for resource, name in ((context, "контекст"), (browser, "браузер")):
                if resource is None:
                    continue
                try:
                    resource.close()
                except Exception as e:
                    logger.warning("Не удалось закрыть %s Playwright: %s", name, e)
            logger.info("Браузер закрыт.")


def save_cookies(file_path: str, data: dict) -> None:
    tmp_path = os.fspath(file_path) + ".tmp"
    with _cookie_write_lock:
        try:
            with open(tmp_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, file_path)
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)


def load_cookies(file_path: str) -> dict:
    with open(file_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    # Всегда возвращаем именно словарь кук (может быть пустым)
    return data.get('cookies', {})
