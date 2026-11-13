import hashlib
import io
import re

import requests
from bs4 import BeautifulSoup
from pdfminer.high_level import extract_text_to_fp
from pdfminer.layout import LAParams
from playwright.sync_api import sync_playwright

from services.cases import DEFAULT_TIMEOUT, session
from services.cookies import BROWSER_LAUNCH_TIMEOUT_MS, PAGE_TIMEOUT_MS


def fetch_case_html(case_url: str, cookies: dict, headers: dict, proxies: dict,
                    timeout: float | tuple[float, float] | None = None) -> str:
    """Скачивает страницу дела с тайм-аутом и повторами (см. services.cases)."""
    timeout = timeout or DEFAULT_TIMEOUT
    try:
        resp = session.get(case_url, cookies=cookies, headers=headers, proxies=proxies, timeout=timeout)
        resp.raise_for_status()
    except requests.HTTPError as e:
        status = getattr(getattr(e, 'response', None), 'status_code', None)
        if status == 451:
            print("----------------------------------------")
            print("HTTP 451 from kad.arbitr.ru. Запустите обновление кук: python main.py --mode update-cookies")
            print("----------------------------------------")
            raise RuntimeError("HTTP 451 while fetching case details")
        raise
    return resp.text


def parse_case_details(html: str) -> dict:
    soup = BeautifulSoup(html, 'html.parser')

    # Длительность заседания: li.case-dur
    duration = None
    dur_li = soup.select_one('li.case-dur')
    if dur_li:
        duration = dur_li.get_text(strip=True)

    # Хронология дела: блок #chrono_list_content, элементы .b-chrono-item-header
    chronology = []
    chrono_root = soup.select_one('#chrono_list_content')
    if chrono_root:
        for item in chrono_root.select('.b-chrono-item-header'):
            inst_title_el = item.select_one('.l-col > strong')
            inst_title = inst_title_el.get_text(strip=True) if inst_title_el else None
            reg_date_el = item.select_one('.l-col .b-reg-date')
            reg_date = reg_date_el.get_text(strip=True) if reg_date_el else None
            incoming_num_el = item.select_one('.l-col .b-reg-incoming_num')
            incoming_num = incoming_num_el.get_text(strip=True) if incoming_num_el else None
            case_num_el = item.select_one('.r-col .b-case-instance-number')
            case_num = case_num_el.get_text(strip=True) if case_num_el else None
            court_name_el = item.select_one('.r-col .instantion-name a')
            court_name = court_name_el.get_text(strip=True) if court_name_el else None
            result_el = item.select_one('.r-col .b-case-result')
            result_name = None
            result_link = None
            if result_el:
                result_name = result_el.get_text(' ', strip=True)
                a = result_el.find('a')
                if a and a.has_attr('href'):
                    result_link = a['href']
            chronology.append({
                'instance': inst_title,
                'date': reg_date,
                'incomingNumber': incoming_num,
                'caseNumber': case_num,
                'court': court_name,
                'result_name': result_name,
                'resultUrl': result_link,
            })

    # Статья/категория дела: dt.b-iblock__header.b-iblock__header_card > span
    article = None
    header_dt = soup.select_one('dt.b-iblock__header.b-iblock__header_card')
    if header_dt:
        span = header_dt.find('span')
        if span:
            article = span.get_text(' ', strip=True)

    # Участники: блок #gr_case_partps по колонкам
    third_parties, other_parties = [], []
    parts_root = soup.select_one('#gr_case_partps')
    if parts_root:
        def collect(td_class: str) -> list:
            vals: list[str] = []
            # Проходим по всем li в соответствующей колонке
            for li in parts_root.select(f'td.{td_class} ul li'):
                # Удаляем скрытый блок с подробностями, чтобы он не попадал в основной текст
                hidden = li.select_one('span.js-rolloverHtml')
                if hidden:
                    hidden.extract()
                # Берем имя участника из ссылки, если есть, иначе из текста li
                a = li.find('a')
                text = a.get_text(' ', strip=True) if a else li.get_text(' ', strip=True)
                if text:
                    vals.append(text)
            return vals
        third_parties = collect('third')
        other_parties = collect('others')

    return {
        'duration': duration,
        'chronology': chronology,
        'article': article,
        'thirdParties': third_parties,
        'otherParties': other_parties,
    }


def fetch_pdf_text(url: str, cookies: dict | None = None, proxies: dict | None = None, headers: dict | None = None, timeout: int = 60) -> str | None:
    if not url:
        print('[pdf] empty url')
        return None

    # 1) Попытка пройти антибот и скачать PDF напрямую через requests
    # try:
    pdf_bytes = _fetch_pdf_via_antibot(url, cookies=cookies, headers=headers, proxies=proxies, timeout=timeout)
    # print(pdf_bytes)
    if pdf_bytes:
        try:
            bio = io.BytesIO(pdf_bytes)
            out = io.StringIO()
            extract_text_to_fp(bio, out, laparams=LAParams(), output_type='text', codec=None)
            text = (out.getvalue() or '').strip()
            return text or None
        except Exception as e1:
            print(f"[pdf] antibot flow error: {e1}")



def _parse_proxy_for_playwright(proxies: dict | None) -> dict | None:
    if not proxies:
        return None
    http = proxies.get('http') or proxies.get('https')
    if not http:
        return None
    # http://user:pass@host:port
    try:
        from urllib.parse import urlparse
        p = urlparse(http)
        proxy = {
            'server': f"{p.scheme}://{p.hostname}:{p.port}",
        }
        if p.username:
            proxy['username'] = p.username
        if p.password:
            proxy['password'] = p.password
        return proxy
    except Exception:
        return None


def _fetch_pdf_via_playwright(url: str, proxies: dict | None, cookies: dict | None) -> bytes | None:
    """Достаёт PDF через браузер.

    Все операции ограничены PAGE_TIMEOUT_MS, а браузер и контекст
    закрываются в finally: раньше при любом исключении процесс Chromium
    оставался в памяти (утечка) и мог зависнуть навсегда.
    """
    proxy_cfg = _parse_proxy_for_playwright(proxies)
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False, proxy=proxy_cfg, timeout=BROWSER_LAUNCH_TIMEOUT_MS
        )
        context = None
        try:
            context = browser.new_context()
            context.set_default_timeout(PAGE_TIMEOUT_MS)
            # Прокидываем куки из cookies.json в контекст браузера
            try:
                if cookies:
                    context.add_cookies([
                        {
                            'name': k,
                            'value': v,
                            'domain': 'kad.arbitr.ru',
                            'path': '/',
                        }
                        for k, v in cookies.items()
                    ])
            except Exception as e:
                print(f"[pdf] add_cookies error: {e}")
            # Патчим createObjectURL, чтобы поймать blob PDF, который viewer создаёт для <embed>
            try:
                context.add_init_script("""
                    (() => {
                      const orig = URL.createObjectURL;
                      window.__pdfCapturedBlobs = [];
                      URL.createObjectURL = function(obj) {
                        try {
                          if (obj && obj.type && String(obj.type).toLowerCase().includes('pdf')) {
                            window.__pdfCapturedBlobs.push(obj);
                          }
                        } catch(e) {}
                        return orig.apply(this, arguments);
                      };
                    })();
                """)
            except Exception:
                pass

            page = context.new_page()
            page.set_default_timeout(PAGE_TIMEOUT_MS)

            # 1) Слушаем сетевые ответы и забираем application/pdf напрямую
            captured_pdf_bytes: list[bytes] = []

            def _on_response(resp):
                try:
                    ct = (resp.headers.get('content-type') or '').lower()
                    url_l = resp.url.lower()
                    if 'application/pdf' in ct or url_l.endswith('.pdf'):
                        body = resp.body()
                        if body:
                            captured_pdf_bytes.append(body)
                except Exception:
                    pass

            context.on('response', _on_response)
            page.on('response', _on_response)

            # Открываем и ждём, чтобы прошли редиректы и инициализировался PDF viewer
            page.goto(url, wait_until='load', timeout=PAGE_TIMEOUT_MS)
            try:
                page.wait_for_load_state('networkidle', timeout=PAGE_TIMEOUT_MS)
            except Exception as e:
                print(f"[pdf] networkidle wait skipped: {e}")
            page.wait_for_timeout(2000)

            # Если поймали PDF по сети — используем его
            if captured_pdf_bytes:
                pdf_bytes = captured_pdf_bytes[0]
                try:
                    with open('downloaded_result_playwright.pdf', 'wb') as f:
                        f.write(pdf_bytes)
                except Exception:
                    pass
                return pdf_bytes

            # 2) Фолбэк: вытащить PDF-blob из viewer (через ранее заинжекченный хук)
            try:
                b64 = page.evaluate("""
                    async () => {
                      try {
                        const blobs = window.__pdfCapturedBlobs || [];
                        if (!blobs.length) return null;
                        const blob = blobs[0];
                        const buf = await blob.arrayBuffer();
                        let binary = '';
                        const bytes = new Uint8Array(buf);
                        const chunkSize = 0x8000;
                        for (let i = 0; i < bytes.length; i += chunkSize) {
                          binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunkSize));
                        }
                        return btoa(binary);
                      } catch (e) {
                        return null;
                      }
                    }
                """)
                if b64:
                    import base64
                    pdf_bytes = base64.b64decode(b64)
                    try:
                        with open('downloaded_result_playwright.pdf', 'wb') as f:
                            f.write(pdf_bytes)
                    except Exception:
                        pass
                    return pdf_bytes
            except Exception:
                pass

            # 3) Последний фолбэк: печать страницы в PDF (может не совпадать с исходным PDF)
            pdf_bytes = page.pdf(print_background=True, prefer_css_page_size=True)
            print(f"[pdf] page.pdf bytes={len(pdf_bytes) if pdf_bytes else 0}")
            try:
                with open('downloaded_page_print.pdf', 'wb') as f:
                    f.write(pdf_bytes or b'')
            except Exception as se:
                print(f"[pdf] save error: {se}")
            return pdf_bytes or None
        finally:
            for resource, name in ((context, "контекст"), (browser, "браузер")):
                if resource is None:
                    continue
                try:
                    resource.close()
                except Exception as e:
                    print(f"[pdf] не удалось закрыть {name}: {e}")


def _fetch_pdf_via_antibot(url: str, cookies: dict | None, headers: dict | None, proxies: dict | None, timeout: int) -> bytes | None:
    # Если сразу отдают PDF — просто вернуть
    resp0 = session.get(url, cookies=cookies, headers=headers, proxies=proxies or {}, timeout=timeout, allow_redirects=True)
    # print(resp0.text)
    ct0 = (resp0.headers.get('Content-Type') or '').lower()
    if 'application/pdf' in ct0 and resp0.ok:
        return resp0.content
    html = resp0.text or ''

    # Парсим token и salto из приманки
    m_token = re.search(r'id=["\']token["\'][^>]*value=["\'](\d+)["\']', html, re.I)
    m_salto = re.search(r'id=["\']salto["\'][^>]*>([^<]+)<', html, re.I)
    if not (m_token and m_salto):
        raise RuntimeError('antibot fields not found')
    token = m_token.group(1)
    salto = m_salto.group(1)
    hash_value = hashlib.md5((token + salto).encode('utf-8')).hexdigest()

    # Сформировать заголовки под отправку формы
    post_headers = {
        'User-Agent': 'Mozilla/5.0 ... Chrome/140 Safari/537.36',
        'Accept': 'application/pdf,application/octet-stream;q=0.9,*/*;q=0.8',
        'Content-Type': 'application/x-www-form-urlencoded',
        'Referer': url,
        'Origin': 'https://kad.arbitr.ru',
    }

    data = {'token': token, 'hash': hash_value}
    resp = session.post(url, data=data, cookies=cookies, headers=post_headers, proxies=proxies or {}, timeout=timeout, allow_redirects=True)
    ct = (resp.headers.get('Content-Type') or '').lower()
    if 'application/pdf' in ct and resp.ok:
        return resp.content
    return None