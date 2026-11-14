import json
import logging
import os
import sys

import requests
import urllib3
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Проверка SSL при запросах к kad.arbitr.ru через прокси:
# - если задан путь к CA-бандлу (.pem) в KAD_CA_BUNDLE (например,
#   Russian Trusted Root CA / Минцифры или CA прокси), проверяем SSL по нему;
# - иначе используем verify=False, чтобы обойти CERTIFICATE_VERIFY_FAILED
#   при работе через резидентный прокси.
KAD_CA_BUNDLE = os.getenv("KAD_CA_BUNDLE", "").strip() or None
SSL_VERIFY: bool | str = KAD_CA_BUNDLE if KAD_CA_BUNDLE else False

if SSL_VERIFY is False:
    # Подавляем предупреждения об отключённой проверке SSL при verify=False.
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# enricher может запускаться и как отдельный CLI из своей папки.
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from config import PROXY_URL
from core.proxy import parse_proxy_url, redact_proxy_error
from services.cookies import load_cookies

# === Сетевые тайм-ауты (секунды) ===
# connect/read разделены: «сайт не отвечает» и «сайт долго думает» —
# разные проблемы, и ждать по 60 секунд на каждую нельзя.
CONNECT_TIMEOUT = float(os.getenv("KAD_CONNECT_TIMEOUT", "10"))
READ_TIMEOUT = float(os.getenv("KAD_READ_TIMEOUT", "45"))
DEFAULT_TIMEOUT = (CONNECT_TIMEOUT, READ_TIMEOUT)

# Повторы только для идемпотентного поиска: сетевые сбои, 429 и 5xx.
MAX_RETRIES = 3
RETRY_BACKOFF_FACTOR = 1.0
RETRY_STATUSES = (429, 500, 502, 503, 504)


class CookieExpiredError(Exception):
    """Куки протухли — нужно обновить через Playwright."""
    pass


class ProxyConnectionError(RuntimeError):
    """Запрос через настроенный прокси не выполнен."""


logger = logging.getLogger(__name__)


def build_session() -> requests.Session:
    """Сессия с повторами и пулом соединений для запросов к kad.arbitr.ru."""
    session = requests.Session()
    retry = Retry(
        total=MAX_RETRIES,
        connect=MAX_RETRIES,
        read=MAX_RETRIES,
        status=MAX_RETRIES,
        backoff_factor=RETRY_BACKOFF_FACTOR,
        status_forcelist=RETRY_STATUSES,
        allowed_methods=frozenset({"GET", "POST", "HEAD"}),
        raise_on_status=False,
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=8)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


# Общая сессия: повторы и пул соединений для всех запросов к сайту суда.
session = build_session()


def fetch_cases_html(url: str, cookies: dict, headers: dict, payload: dict,
                     proxies: dict | None, timeout: float | tuple[float, float] | None = None,
                     verify: bool | str | None = None) -> str:
    """Запрашивает страницу поиска дел.

    Всегда с тайм-аутом (по умолчанию CONNECT_TIMEOUT/READ_TIMEOUT) и с
    автоматическими повторами для сетевых ошибок и 429/5xx.
    """
    logger.info(f"Requesting cases: url={url} page={payload.get('Page')} count={payload.get('Count')}")
    timeout = timeout or DEFAULT_TIMEOUT
    ssl_verify = SSL_VERIFY if verify is None else verify
    proxy_url = (proxies or {}).get("https") or (proxies or {}).get("http")

    try:
        resp = session.post(
            url, cookies=cookies, headers=headers, json=payload,
            proxies=proxies, timeout=timeout, verify=ssl_verify,
        )
        resp.raise_for_status()
    except requests.HTTPError as e:
        status = getattr(getattr(e, 'response', None), 'status_code', None)
        if status == 451:
            raise CookieExpiredError("Куки протухли (HTTP 451)")
        if status == 407:
            safe_error = redact_proxy_error(e, proxy_url)
            logger.error("Прокси требует авторизацию (HTTP 407): %s", safe_error)
            raise ProxyConnectionError(
                f"Прокси требует авторизацию (HTTP 407): {safe_error}"
            ) from e
        logger.error("KAD ответил ошибкой статуса %s", status)
        raise
    except requests.ProxyError as e:
        safe_error = redact_proxy_error(e, proxy_url)
        logger.error("Ошибка подключения к прокси: %s", safe_error)
        raise ProxyConnectionError(f"Ошибка подключения к прокси: {safe_error}") from e
    except requests.Timeout as e:
        safe_error = redact_proxy_error(e, proxy_url)
        if proxy_url:
            logger.error("Тайм-аут запроса через прокси (%s с): %s", timeout, safe_error)
            raise ProxyConnectionError(
                f"Тайм-аут запроса через прокси ({timeout} с): {safe_error}"
            ) from e
        logger.error("Тайм-аут запроса к KAD (%s с): %s", timeout, safe_error)
        raise
    except requests.ConnectionError as e:
        safe_error = redact_proxy_error(e, proxy_url)
        if proxy_url:
            logger.error("Ошибка соединения через прокси: %s", safe_error)
            raise ProxyConnectionError(f"Ошибка соединения через прокси: {safe_error}") from e
        logger.error("Сетевая ошибка при запросе к KAD: %s", safe_error)
        raise
    except requests.RequestException as e:
        safe_error = redact_proxy_error(e, proxy_url)
        logger.error("Сетевая ошибка при запросе к KAD: %s", safe_error)
        raise

    logger.info(f"Cases response ok: status={resp.status_code} bytes={len(resp.text)}")
    return resp.text


def parse_cases(html: str) -> list:

    soup = BeautifulSoup(html, 'html.parser')
    rows = []
    seen_cases = set()

    for case_a in soup.find_all('a', class_='num_case'):
        tr = case_a.find_parent('tr')
        if tr is None:
            continue

        num_td = tr.find('td', class_='num')
        court_td = tr.find('td', class_='court')
        pl_td = tr.find('td', class_='plaintiff')
        resp_td = tr.find('td', class_='respondent')

        date_span = num_td.find('span') if num_td else None
        date_text = date_span.get_text(strip=True) if date_span else None
        case_number = case_a.get_text(strip=True)
        case_url = case_a.get('href', '').strip()

        # Одно дело может быть отрисовано несколькими ссылками —
        # дубликаты отбрасываем по номеру дела.
        key = (case_number, case_url)
        if key in seen_cases:
            continue
        seen_cases.add(key)

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

    proxy_settings = parse_proxy_url(PROXY_URL)
    proxies = proxy_settings.as_requests() if proxy_settings else None

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
              date_from: str | None = None, date_to: str | None = None,
              *, include_html: bool = False):
    """Возвращает дела и контекст; include_html добавляет исходный HTML в конец."""
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
    result = (rows, cookies_dict, headers, proxies, pages_count)
    return (*result, html) if include_html else result


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
