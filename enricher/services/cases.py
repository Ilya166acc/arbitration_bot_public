import json
import logging
import os
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from services.cookies import load_cookies

try:
    from config import PROXY_URL
except ImportError:  # запуск enricher как самостоятельного проекта (cd enricher)
    PROXY_URL = os.getenv("PROXY_URL", "").strip() or None

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


logger = logging.getLogger(__name__)

SUPPORTED_PROXY_SCHEMES = {"http", "https", "socks5", "socks5h", "socks4", "socks4a"}

# Чтобы при каждом запросе не писать одну и ту же ошибку в лог.
_proxy_warning_logged = False


def _get_proxy_url() -> str | None:
    """Возвращает актуальный PROXY_URL (config или переменная окружения)."""
    return PROXY_URL or os.getenv("PROXY_URL", "").strip() or None


def _mask_proxy_url(proxy_url: str | None) -> str:
    """Скрывает логин/пароль прокси в сообщениях логов."""
    if not proxy_url:
        return "<не задан>"
    try:
        parsed = urlparse(proxy_url)
        host = parsed.hostname or "?"
        port = f":{parsed.port}" if parsed.port else ""
        if parsed.username or parsed.password:
            return f"{parsed.scheme or 'http'}://***:***@{host}{port}"
    except ValueError:
        pass
    return "<некорректный URL>"


def build_proxies(proxy_url: str | None = None) -> dict | None:
    """Собирает словарь proxies для requests из PROXY_URL.

    Пример: http://user:pass@ru.proxy.rucaptcha.com:2334
    -> {"http": "...", "https": "..."}

    Если PROXY_URL не задан или некорректен — логирует ошибку и возвращает
    None: запрос пойдёт напрямую (если сеть это позволяет).
    """
    global _proxy_warning_logged

    url = proxy_url if proxy_url is not None else _get_proxy_url()
    if not url:
        if not _proxy_warning_logged:
            logger.error(
                "PROXY_URL не задан — запросы к kad.arbitr.ru идут напрямую, без прокси. "
                "Если сайт блокирует ваш регион, задайте PROXY_URL."
            )
            _proxy_warning_logged = True
        return None

    try:
        parsed = urlparse(url)
        scheme = (parsed.scheme or "").lower()
        if scheme not in SUPPORTED_PROXY_SCHEMES:
            raise ValueError(f"неподдерживаемая схема «{scheme or 'нет'}»")
        if not parsed.hostname:
            raise ValueError("не указан хост прокси")
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError(f"некорректный порт ({exc})") from exc
        if not port:
            raise ValueError("не указан порт прокси")
    except ValueError as exc:
        if not _proxy_warning_logged:
            logger.error(
                "Некорректный PROXY_URL %s: %s. Запросы пойдут напрямую, без прокси.",
                _mask_proxy_url(url), exc,
            )
            _proxy_warning_logged = True
        return None

    logger.info("Использую прокси %s", _mask_proxy_url(url))
    return {"http": url, "https": url}


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
                     proxies: dict | None, timeout: float | tuple[float, float] | None = None) -> str:
    """Запрашивает страницу поиска дел.

    Всегда с тайм-аутом (по умолчанию CONNECT_TIMEOUT/READ_TIMEOUT) и с
    автоматическими повторами для сетевых ошибок и 429/5xx.
    """
    logger.info(f"Requesting cases: url={url} page={payload.get('Page')} count={payload.get('Count')}")
    timeout = timeout or DEFAULT_TIMEOUT

    try:
        resp = session.post(
            url, cookies=cookies, headers=headers, json=payload,
            proxies=proxies, timeout=timeout,
        )
        resp.raise_for_status()
    except requests.HTTPError as e:
        status = getattr(getattr(e, 'response', None), 'status_code', None)
        if status == 451:
            raise CookieExpiredError("Куки протухли (HTTP 451)")
        logger.error("KAD ответил ошибкой статуса %s", status)
        raise
    except requests.Timeout as e:
        logger.error("Тайм-аут запроса к KAD (%s с): %s", timeout, e)
        raise
    except requests.exceptions.ProxyError as e:
        logger.error(
            "Ошибка прокси %s при запросе к KAD: %s. "
            "Проверьте PROXY_URL, логин/пароль и доступность прокси.",
            _mask_proxy_url(_get_proxy_url()), e,
        )
        raise
    except requests.RequestException as e:
        logger.error("Сетевая ошибка при запросе к KAD: %s", e)
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
        'Accept': 'application/json, text/javascript, */*; q=0.01',
        'Accept-Language': 'ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7',
        'X-Requested-With': 'XMLHttpRequest',
        'Origin': 'https://kad.arbitr.ru',
        'Referer': 'https://kad.arbitr.ru/',
    }

    proxies = build_proxies()

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
