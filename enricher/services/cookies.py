from playwright.sync_api import sync_playwright
import json
import os
from threading import Lock

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
        }
        if proxy:
            launch_kwargs["proxy"] = proxy

        print("DEBUG: Запускаю браузер...")
        browser = p.chromium.launch(**launch_kwargs)
        
        try:
            context = browser.new_context(
                user_agent=user_agent,
                locale='ru-RU',
                timezone_id='Europe/Moscow',
                ignore_https_errors=True,
                viewport={"width": 1366, "height": 768},
            )
            page = context.new_page()

            print("DEBUG: Перехожу на сайт...")
            try:
                page.goto(start_url, wait_until='domcontentloaded', timeout=15000) # Ждем максимум 15 секунд
                page.wait_for_timeout(5000)
            except Exception as e:
                print(f"DEBUG: Ошибка загрузки страницы: {e}")

            # === НОВЫЙ БЛОК: Имитация активности человека ===
            print("DEBUG: Имитирую активность пользователя...")
            try:
                # Двигаем мышь и кликаем по пустому месту
                page.mouse.move(500, 300)
                page.mouse.click(500, 300)
                page.wait_for_timeout(1000)
                
                # Скроллим страницу вниз
                page.mouse.wheel(0, 500)
                page.wait_for_timeout(1000)
                
                # Пробуем найти кнопку "Найти" (если она есть на странице)
                find_btn = page.locator('button[type="submit"]', has_text="Найти")
                if find_btn.first.is_visible(timeout=3000):
                    find_btn.first.click()
                    page.wait_for_timeout(8000) # Ждем 8 секунд после клика
            except Exception as e:
                print(f"DEBUG: Ошибка при имитации кликов: {e}")
            # ====================================================

            print("DEBUG: Собираю куки...")
            cookie_list = context.cookies()
            cookie_dict = {c['name']: c['value'] for c in cookie_list}

            print(f"DEBUG: Собрано кук: {len(cookie_dict)}")
            return {"cookies": cookie_dict, "cookiesRaw": cookie_list}
            
        except Exception as e:
            print(f"DEBUG: Критическая ошибка внутри Playwright: {e}")
            return {"cookies": {}, "cookiesRaw": []}
        finally:
            # ГАРАНТИРОВАННО закрываем браузер, чтобы он не висел в памяти
            browser.close()
            print("DEBUG: Браузер закрыт.")


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
