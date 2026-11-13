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
NAVIGATION_SETTLE_MS = 5000
ACTION_SETTLE_MS = 1000
CLICK_RESULT_WAIT_MS = 8000
VISIBILITY_TIMEOUT_MS = 2000

_cookie_write_lock = Lock()


def get_cookies_via_browser(start_url: str, proxy: dict | None, user_agent: str,
                            headless: bool = False, offscreen: bool = False) -> dict:
    with sync_playwright() as p:
        args = ['--disable-blink-features=AutomationControlled']
        if headless:
            args.append('--headless=new')
        elif offscreen:
            args.extend(['--window-position=-32000,-32000', '--window-size=1366,768'])

        launch_kwargs = {
            "headless": False,
            "args": args,
            "timeout": BROWSER_LAUNCH_TIMEOUT_MS,
        }
        if proxy:
            launch_kwargs["proxy"] = proxy
            logger.info(f"🌐 Использую прокси: {proxy.get('server', 'unknown')}") # <--- Логирование прокси!
        else:
            logger.info("⚠️ Прокси НЕ используется, прямое подключение.")

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
            context.set_default_timeout(PAGE_TIMEOUT_MS)
            page = context.new_page()
            page.set_default_timeout(PAGE_TIMEOUT_MS)

            logger.info("Перехожу на %s", start_url)
            try:
                page.goto(start_url, wait_until='domcontentloaded', timeout=PAGE_TIMEOUT_MS)
                page.wait_for_timeout(NAVIGATION_SETTLE_MS)
            except Exception as e:
                logger.warning("Не удалось загрузить страницу: %s", e)

            # === УЛЬТИМАТИВНАЯ ОЧИСТКА ОТ БАННЕРОВ (НАШ ФИКС) ===
            try:
                page.evaluate("""
                    const selectors = ['.b-promo_notification-popup_wrapper', '.b-promo_notification-popup', '.b-promo_notification', '.modal-backdrop', '[class*="overlay"]', '[class*="popup"]'];
                    selectors.forEach(sel => {
                        document.querySelectorAll(sel).forEach(el => el.remove());
                    });
                    document.body.style.overflow = 'auto';
                """)
                page.wait_for_timeout(1000)
            except Exception as e:
                logger.warning("Ошибка при очистке баннеров: %s", e)

            # === ИМИТАЦИЯ И ПОИСК (НАШ ФИКС) ===
            try:
                page.mouse.move(500, 300)
                page.mouse.click(500, 300)
                page.wait_for_timeout(ACTION_SETTLE_MS)
                page.mouse.wheel(0, 500)
                page.wait_for_timeout(ACTION_SETTLE_MS)

                find_btn = page.locator('button[type="submit"]', has_text="Найти")
                if find_btn.first.is_visible(timeout=VISIBILITY_TIMEOUT_MS):
                    # force=True обходит любые невидимые преграды
                    find_btn.first.click(force=True) 
                    logger.info("Нажали кнопку 'Найти', ждем загрузки результатов...")
                    
                    try:
                        page.wait_for_selector('a.num_case, .b-found-nothing', timeout=15000)
                        logger.info("Результаты поиска успешно загрузились!")
                    except Exception:
                        logger.warning("Не дождались таблицы результатов.")
                        
                    page.wait_for_timeout(3000)
            except Exception as e:
                logger.warning("Ошибка при поиске: %s", e)

            logger.info("Собираю куки...")
            cookie_list = context.cookies()
            cookie_dict = {c['name']: c['value'] for c in cookie_list}
            logger.info("Собрано кук: %d", len(cookie_dict))
            return {"cookies": cookie_dict, "cookiesRaw": cookie_list}

        except Exception as e:
            logger.error("Критическая ошибка внутри Playwright: %s", e)
            return {"cookies": {}, "cookiesRaw": []}
        finally:
            for resource, name in ((context, "контекст"), (browser, "браузер")):
                if resource is None:
                    continue
                try:
                    resource.close()
                except Exception as e:
                    logger.warning("Не удалось закрыть %s Playwright: %s", name, e)
            logger.info("Браузер закрыт.")


def load_cookies(file_path: str) -> dict:
    with open(file_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    # Всегда возвращаем именно словарь кук (может быть пустым)
    return data.get('cookies', {})
