from playwright.sync_api import sync_playwright
import json


def get_cookies_via_browser(start_url: str, proxy: dict | None, user_agent: str,
                             headless: bool = False, offscreen: bool = False) -> dict:
    with sync_playwright() as p:
        args = ['--disable-blink-features=AutomationControlled']

        # Если headless=True, используем новый режим Chrome (меньше палится)
        if headless:
            args.append('--headless=new')
        elif offscreen:
            args.extend(['--window-position=-32000,-32000', '--window-size=1366,768'])

        launch_kwargs = {
            "headless": False, # <--- ВАЖНО: Всегда False, режим задаем через args выше
            "args": args,
        }
        if proxy:
            launch_kwargs["proxy"] = proxy
        # ... дальше без изменений

        browser = p.chromium.launch(**launch_kwargs)
        context = browser.new_context(
            user_agent=user_agent,
            locale='ru-RU',
            timezone_id='Europe/Moscow',
            ignore_https_errors=True,
            viewport={"width": 1366, "height": 768},
        )
        page = context.new_page()

        try:
            page.goto(start_url, wait_until='domcontentloaded', timeout=30000)
            page.wait_for_timeout(10000)   # Ждем 10 секунд, чтобы DDoS-Guard выдал все куки
        except Exception as e:
            print(f"Ошибка загрузки страницы: {e}")

        # Закрываем промо-баннер
        try:
            promo_close = page.locator('a.b-promo_notification-popup-close.js-promo_notification-popup-close')
            if promo_close.first.is_visible(timeout=2000):
                promo_close.first.click()
        except Exception:
            pass

        # Нажимаем "Найти", чтобы имитировать реального пользователя
        try:
            find_btn = page.locator('button[type="submit"]', has_text="Найти")
            if find_btn.first.is_visible(timeout=5000):
                find_btn.first.click()
                page.wait_for_timeout(7000) # Ждем 7 секунд после клика
        except Exception:
            pass

        cookie_list = context.cookies()
        cookie_dict = {c['name']: c['value'] for c in cookie_list}

        print(f"Собрано кук: {len(cookie_dict)}")
        for name in cookie_dict:
            print(f"  - {name}")

        browser.close()
        return {"cookies": cookie_dict, "cookiesRaw": cookie_list}


def save_cookies(file_path: str, data: dict) -> None:
	with open(file_path, 'w', encoding='utf-8') as f:
		json.dump(data, f, ensure_ascii=False, indent=2)


def load_cookies(file_path: str) -> dict:
    with open(file_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    # Всегда возвращаем именно словарь кук (может быть пустым)
    return data.get('cookies', {})
