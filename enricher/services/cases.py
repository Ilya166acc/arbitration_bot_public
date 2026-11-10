import requests
from bs4 import BeautifulSoup
import json
import os
from services.cookies import load_cookies
import logging
import sys

class CookieExpiredError(Exception):
    """Куки протухли — нужно обновить через Playwright."""
    pass
logger = logging.getLogger(__name__)


def fetch_cases_html(url: str, cookies: dict, headers: dict, payload: dict, proxies: dict | None) -> str:
    logger.info(f"Requesting cases: url={url} page={payload.get('Page')} count={payload.get('Count')}")

    # === ПРЕОБРАЗУЕМ PAYLOAD В FORM-DATA ===
    form_data = {}
    for key, value in payload.items():
        if key == "Sides":
            # Превращаем список объектов в JSON-строку
            form_data[key] = json.dumps(value, ensure_ascii=False)
        elif value is not None:
            # Остальные поля оставляем как есть (или превращаем в строку)
            form_data[key] = str(value)
        else:
            # None заменяем на пустую строку, если это нужно
            form_data[key] = ''
    # =====================================

    try:
        # Отправляем form_data, а не payload
        resp = requests.post(url, cookies=cookies, headers=headers, json=payload, proxies=proxies, timeout=60)
        resp.raise_for_status()
    except requests.HTTPError as e:
        status = getattr(getattr(e, 'response', None), 'status_code', None)
        if status == 451:
            raise CookieExpiredError("Куки протухли (HTTP 451)")
        raise
    logger.info(f"Cases response ok: status={resp.status_code} bytes={len(resp.text)}")
    return resp.text


def parse_cases(html: str) -> list:

    soup = BeautifulSoup(html, 'html.parser')
    rows = []
    seen_tr = set()

    for case_a in soup.find_all('a', class_='num_case'):
        tr = case_a.find_parent('tr')
        if tr is None:
            continue
        key = id(tr)
        if key in seen_tr:
            continue
        seen_tr.add(key)

        num_td = tr.find('td', class_='num')
        court_td = tr.find('td', class_='court')
        pl_td = tr.find('td', class_='plaintiff')
        resp_td = tr.find('td', class_='respondent')

        date_span = num_td.find('span') if num_td else None
        date_text = date_span.get_text(strip=True) if date_span else None
        case_number = case_a.get_text(strip=True)
        case_url = case_a.get('href', '').strip()

        judge_div = court_td.find('div', class_='judge') if court_td else None
        judge = judge_div.get_text(strip=True) if judge_div else None
        court_name = None
        if court_td:
            for d in court_td.find_all('div'):
                if d is judge_div:
                    continue
                if d.has_attr('title'):
                    court_name = d['title']
                    break

        plaintiffs = []
        if pl_td:
            for s in pl_td.find_all('span', class_='js-rollover'):
                # Удаляем скрытый span, чтобы не дублировать текст
                hidden = s.find('span', class_='js-rolloverHtml')
                if hidden:
                    hidden.decompose()
                text = s.get_text(' ', strip=True)
                if text and text.lower() != 'подробнее...':
                    plaintiffs.append(text)

        respondents = []
        if resp_td:
            for s in resp_td.find_all('span', class_='js-rollover'):
                hidden = s.find('span', class_='js-rolloverHtml')
                if hidden:
                    hidden.decompose()
                text = s.get_text(' ', strip=True)
                if text and text.lower() != 'подробнее...':
                    respondents.append(text)

        rows.append({
            'date': date_text,
            'caseNumber': case_number,
            'caseUrl': case_url,
            'court': court_name,
            'judge': judge,
            'plaintiffs': plaintiffs,
            'respondents': respondents,
        })

    logger.info(f"Parsed cases: count={len(rows)}")
    return rows


def parse_pages_count(html: str) -> int:
    soup = BeautifulSoup(html, 'html.parser')
    inp = soup.find('input', id='documentsPagesCount')
    if inp and inp.has_attr('value'):
        try:
            return int(inp['value'])
        except Exception:
            return 1
    return 1


def build_cases_request(page: int, count: int, courts: list[str] | None = None, 
                        sides: list[str] | None = None, 
                        date_from: str | None = None, 
                        date_to: str | None = None):
    
    logger.info(f"Building cases request: page={page} count={count} courts={courts} sides={sides} dates={date_from}-{date_to}")
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Content-Type': 'application/json',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
        'Accept-Language': 'ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7',
        'Cache-Control': 'no-cache',
        'Host': 'kad.arbitr.ru',
    }

    proxies = None

    payload = {
        "Page": page,
        "Count": count,
        "Courts": courts if courts is not None else [],
        "DateFrom": date_from,
        "DateTo": date_to,
        "Sides": [{"Name": s, "Type": -1, "ExactMatch": False} for s in (sides or [])],
        "Judges": [],
        "CaseNumbers": [],
        "WithVKSInstances": False,
    }
    search_url = "https://kad.arbitr.ru/Kad/SearchInstances"
    return search_url, headers, payload, proxies


def get_cases(cookies_file: str, page: int = 1, count: int = 25,
              courts: list[str] | None = None, sides: list[str] | None = None,
              date_from: str | None = None, date_to: str | None = None):
    """Возвращает дела + контекст запроса."""
    if not os.path.exists(cookies_file):
        raise FileNotFoundError(f"Файл кук не найден: {cookies_file}")
    logger.info(f"Loading cookies from {cookies_file}")
    cookies_dict = load_cookies(cookies_file)
    
    search_url, headers, payload, proxies = build_cases_request(page, count, courts=courts, sides=sides, date_from=date_from, date_to=date_to)
    
    logger.info("Fetching cases HTML")
    html = fetch_cases_html(search_url, cookies_dict, headers, payload, proxies)
    rows = parse_cases(html)
    pages_count = parse_pages_count(html)
    logger.info(f"Pages count detected: {pages_count}")
    return rows, cookies_dict, headers, proxies, pages_count


def run_cases(cookies_file: str, out_file: str, page: int = 1, count: int = 25,
              courts: list[str] | None = None, sides: list[str] | None = None) -> int:
    """Прогоняет поиск и сохраняет результаты в JSON."""
    logger.info("Run cases: starting")
    rows, _, _, _, _ = get_cases(cookies_file=cookies_file, page=page, count=count,
                                  courts=courts, sides=sides)
    with open(out_file, 'w', encoding='utf-8') as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)
    logger.info(f"Записано {len(rows)} записей в {out_file}")
    return len(rows)