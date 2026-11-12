import argparse
import logging
import asyncio
import os           
import subprocess  
from services.cookies import get_cookies_via_browser, save_cookies
from services.cases import run_cases, get_cases
from services.case_details import fetch_case_html, parse_case_details, fetch_pdf_text


def run() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s %(name)s: %(message)s'
    )
    logger = logging.getLogger('enricher')
    parser = argparse.ArgumentParser(description='KAD enricher: обновление кук и получение дел')
    parser.add_argument('--mode', choices=['cases', 'update-cookies'], default='cases', help='cases: получить дела; update-cookies: обновить куки через браузер; set-cookies: записать куки из JSON')
    parser.add_argument('--cookies-file', default='cookies.json', help='Путь к файлу с куками для чтения/записи (по умолчанию cookies.json)')
    parser.add_argument('--out', default='results.json', help='Файл для записи JSON дел')
    parser.add_argument('--page', type=int, default=1, help='Начальная страница (deprecated, используйте --start-page)')
    parser.add_argument('--start-page', type=int, default=1, help='Стартовая страница пагинации')
    parser.add_argument('--end-page', type=int, default=9999, help='Финальная страница пагинации (включительно)')
    parser.add_argument('--courts-file', default='courts.json', help='JSON со списком судов для обхода (code, name)')
    args = parser.parse_args()

    # Стартовый URL фиксированный (не требуется передавать)
    start_url = 'https://kad.arbitr.ru/'

    # Параметры прокси с авторизацией
    proxy = None

    # Реалистичные заголовки
    user_agent = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )


    if args.mode == 'update-cookies':
        logger.info('Mode: update-cookies')
        data = get_cookies_via_browser(start_url, proxy, user_agent)
        save_cookies(args.cookies_file, data)
        logger.info(f"Куки обновлены и сохранены в {args.cookies_file}")
        return

    if args.mode == 'cases':
        logger.info('Mode: cases')
        # Инициализируем/загружаем текущие результаты для инкрементальной записи
        import os as _os
        import json as _json
        existing_list = []
        if _os.path.exists(args.out):
            try:
                with open(args.out, 'r', encoding='utf-8') as _f:
                    existing_list = _json.load(_f) or []
            except Exception:
                existing_list = []
        # Индекс по URL дела для быстрого поиска и избежания дублей
        by_url = { (item.get('caseUrl') or ''): item for item in existing_list if isinstance(item, dict) and item.get('caseUrl') }

        # Цикл по страницам
        start_page = args.start_page or args.page or 1
        end_page = args.end_page or 9999
        all_rows = list(by_url.values())
        cookies_dict = None
        headers = None
        proxies = None
        detected_pages = None
        # Загружаем список судов
        import json as _json
        try:
            with open(args.courts_file, 'r', encoding='utf-8') as _cf:
                courts_list = _json.load(_cf)
        except Exception:
            courts_list = []

        for court in courts_list or [{"code": "VAS", "name": "Высший Арбитражный Суд РФ"}]:
            code = (court.get('code') if isinstance(court, dict) else None) or 'VAS'
            name = (court.get('name') if isinstance(court, dict) else None) or code
            logger.info(f"Court loop: {code} - {name}")
            for current_page in range(start_page, end_page + 1):
                logger.info(f"Page loop: fetching page {current_page} for court {code}")
                rows, cookies_dict, headers, proxies, pages_count = get_cases(
                    cookies_file=args.cookies_file,
                    page=current_page,
                    count=25,
                    courts=[code],
                )
                if detected_pages is None:
                    detected_pages = pages_count
                    logger.info(f"Detected total pages (from HTML): {detected_pages}")
                # Останавливаемся, если вышли за предел обнаруженного количества страниц
                if current_page > pages_count:
                    logger.info("Current page exceeds detected total pages for this court. Moving to next court.")
                    break

                logger.info(f"Fetched cases on page {current_page}: count={len(rows)}")
                total = len(rows)
                for idx, item in enumerate(rows, start=1):
                    case_url = item.get('caseUrl')
                    if not case_url:
                        continue
                    # Если уже есть в результатах и есть details — пропускаем
                    existing_item = by_url.get(case_url)
                    if existing_item and isinstance(existing_item, dict) and existing_item.get('details'):
                        logger.info(f"[p{current_page}] [{idx}/{total}] Skip (already have details): caseNumber={existing_item.get('caseNumber')} url={case_url}")
                        continue

                    logger.info(f"[p{current_page}] [{idx}/{total}] Fetching details: caseNumber={item.get('caseNumber')} url={case_url}")
                    try:
                        html = fetch_case_html(case_url, cookies=cookies_dict, headers=headers, proxies=proxies)
                        details = parse_case_details(html)
                        # Для каждого элемента хронологии, если есть resultUrl, скачиваем PDF и кладём текст в result
                        for chrono_item in details.get('chronology', []):
                            pdf_url = chrono_item.get('resultUrl')
                            if pdf_url and not chrono_item.get('result'):
                                try:
                                    chrono_item['result'] = fetch_pdf_text(pdf_url, cookies=cookies_dict, proxies=proxies, headers=headers)
                                except Exception as pdf_e:
                                    chrono_item['resultError'] = str(pdf_e)
                        item['details'] = details
                        logger.info(f"[p{current_page}] [{idx}/{total}] Details parsed: caseNumber={item.get('caseNumber')} chronology_items={len(details.get('chronology', []))}")
                        # Обновляем индекс/лист и сразу пишем на диск
                        if not existing_item:
                            by_url[case_url] = item
                            all_rows.append(item)
                        else:
                            # Обновляем существующую запись новыми полями
                            existing_item.update(item)
                        with open(args.out, 'w', encoding='utf-8') as f:
                            _json.dump(all_rows, f, ensure_ascii=False, indent=2)
                    except Exception as e:
                        item['detailsError'] = str(e)
                        logger.error(f"[p{current_page}] [{idx}/{total}] Details error: caseNumber={item.get('caseNumber')} error={e}")
                        # Даже при ошибке добавим запись (если новой не было) без details, чтобы фиксировать прогресс
                        if not existing_item:
                            by_url[case_url] = item
                            all_rows.append(item)
                            with open(args.out, 'w', encoding='utf-8') as f:
                                _json.dump(all_rows, f, ensure_ascii=False, indent=2)

                # Добавим метаданные по странице без дублирования (записи без деталей, если их ещё не было)
                for item in rows:
                    url_k = item.get('caseUrl')
                    if not url_k:
                        continue
                    if url_k not in by_url:
                        by_url[url_k] = item
                        all_rows.append(item)
                with open(args.out, 'w', encoding='utf-8') as f:
                    _json.dump(all_rows, f, ensure_ascii=False, indent=2)

        logger.info(f"Записано {len(all_rows)} записей в {args.out}")
        return

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
                logger.info("⏳ Обнаружено обновление Playwright или отсутствие Chromium.")
                logger.info("Скачиваю браузер (это займет 1-2 минуты)...")
                subprocess.run(
                    ["playwright", "install", "chromium"],
                    check=True,
                    timeout=900,  # установка не должна вешать запуск бота
                )
                logger.info("✅ Chromium успешно установлен!")
    except Exception as e:
        logger.warning(f"⚠️ Предупреждение при проверке Playwright: {e}")

if __name__ == "__main__":
    # Проверяем браузер ДО запуска бота
    ensure_playwright_browser()    # <--- ДОБАВИТЬ

    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("🛑 Бот остановлен.")