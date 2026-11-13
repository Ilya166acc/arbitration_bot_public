"""Offline regression tests; no Telegram, browser or KAD access required."""
import ast
import asyncio
import concurrent.futures
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from html import escape
import json
import logging
import os
from pathlib import Path
import platform
import random
import tempfile
import threading
import time
from threading import Lock
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]


def functions(path, names, **namespace):
    tree = ast.parse((ROOT / path).read_text())
    tree.body = [node for node in tree.body if isinstance(
        node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names]
    for node in tree.body:
        node.decorator_list = []
    exec(compile(tree, str(path), 'exec'), namespace)
    return namespace


class SecurityFixTests(unittest.TestCase):
    def test_inn_checksums_and_html(self):
        ns = functions('bot/handlers.py', {'is_valid_inn', 'html_value'}, escape=escape)
        for inn in ('7707083893', '500100732259'):
            self.assertTrue(ns['is_valid_inn'](inn))
            self.assertFalse(ns['is_valid_inn'](inn[:-1] + str((int(inn[-1]) + 1) % 10)))
        for inn in ('<b>123</b>', '１２３４５６７８９０', '123', ''):
            self.assertFalse(ns['is_valid_inn'](inn))
        self.assertEqual(ns['html_value']('<b>A&B_*'), '&lt;b&gt;A&amp;B_*')
        self.assertEqual(ns['html_value'](None), '—')

    def test_add_days_and_command_object(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        add_client = Mock(return_value=True)
        ns = functions('bot/handlers.py', {'cmd_add', 'is_valid_inn', 'html_value'},
                       Message=object, CommandObject=object, escape=escape,
                       add_client=add_client)
        for args in ('7707083893 0', '7707083893 366', '7707083893 nope'):
            message = SimpleNamespace(from_user=SimpleNamespace(id=1), answer=AsyncMock())
            asyncio.run(ns['cmd_add'](message, SimpleNamespace(args=args)))
        add_client.assert_not_called()
        for days in (1, 365):
            message = SimpleNamespace(from_user=SimpleNamespace(id=1), answer=AsyncMock())
            asyncio.run(ns['cmd_add'](message, SimpleNamespace(args=f'7707083893 {days}')))
            self.assertEqual(add_client.call_args.args[-1], days)

    def test_challenge_retry_empty_results_and_pagination(self):
        class CookieExpiredError(Exception):
            pass
        refresh = Mock()
        clock = Mock()
        get_cases = Mock()
        ns = functions('core/parser.py', {'fetch_cases_by_inn'},
                       COOKIES_FILE='unused', datetime=datetime, timedelta=timedelta,
                       ZoneInfo=ZoneInfo, logger=logging.getLogger(__name__),
                       get_cases=get_cases, CookieExpiredError=CookieExpiredError,
                       refresh_cookies=refresh, time=clock, random=random)
        for html in ('', '<html>DDoS-Guard</html>'):
            get_cases.return_value = ([], {}, {}, None, 1, html)
            with self.assertRaises(CookieExpiredError):
                ns['fetch_cases_by_inn']('7707083893')
        self.assertEqual(refresh.call_count, 2)
        get_cases.return_value = ([], {}, {}, None, 1, 'Дела не найдены')
        self.assertEqual(ns['fetch_cases_by_inn']('7707083893'), [])
        clock.reset_mock()
        get_cases.side_effect = [([{'caseNumber': 'A'}], {}, {}, None, 2, 'num_case'),
                                ([{'caseNumber': 'B'}], {}, {}, None, 2, 'num_case')]
        self.assertEqual(len(ns['fetch_cases_by_inn']('7707083893')), 2)
        delay = clock.sleep.call_args.args[0]
        self.assertTrue(2 <= delay <= 4)

    def test_atomic_cookie_file(self):
        ns = functions('enricher/services/cookies.py', {'save_cookies', 'load_cookies'},
                       os=os, json=json, _cookie_write_lock=Lock())
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'cookies.json')
            ns['save_cookies'](path, {'cookies': {'session': 'old'}})
            ns['save_cookies'](path, {'cookies': {'session': 'new'}})
            self.assertEqual(ns['load_cookies'](path), {'session': 'new'})
            self.assertFalse(os.path.exists(path + '.tmp'))

    def test_checker_nullable_fields_and_client_delay(self):
        clock = Mock()
        add_case = Mock(return_value=True)
        ns = functions('core/checker.py', {'check_all_clients'}, Dict=dict, List=list,
                       get_all_clients=lambda _: [dict(inn='1', name='A'), dict(inn='2', name='B')],
                       fetch_cases_by_inn=lambda *a, **kw: [dict(caseNumber='X', plaintiffs=None, respondents=None)],
                       case_exists=lambda *a: False, add_case=add_case, time=clock,
                       random=random, logger=logging.getLogger(__name__), CookieExpiredError=RuntimeError)
        results, errors = ns['check_all_clients'](1)
        self.assertFalse(errors)
        self.assertEqual(results['A'][0]['court'], '—')
        self.assertEqual(results['A'][0]['receipt_date'], '—')
        self.assertEqual(results['A'][0]['plaintiff'], '')
        clock.sleep.assert_called_once()


class ReliabilityTests(unittest.TestCase):
    """Регрессии по зависаниям: тайм-ауты Playwright, фоновые проверки, cleanup."""

    # ---------- Playwright: тайм-ауты и закрытие браузера ----------

    @staticmethod
    def _browser_stubs():
        page = Mock()
        page.locator.return_value.first.is_visible.return_value = False
        context = Mock()
        context.new_page.return_value = page
        context.cookies.return_value = [{'name': 'a', 'value': 'b'}]
        browser = Mock()
        browser.new_context.return_value = context
        playwright = Mock()
        playwright.chromium.launch.return_value = browser
        return page, context, browser, playwright

    @staticmethod
    def _cookie_namespace(playwright):
        class PlaywrightContext:
            def __enter__(self):
                return playwright

            def __exit__(self, *exc_info):
                return False

        return functions(
            'enricher/services/cookies.py', {'get_cookies_via_browser'},
            sync_playwright=lambda: PlaywrightContext(),
            logger=logging.getLogger(__name__),
            PAGE_TIMEOUT_MS=30000,
            BROWSER_LAUNCH_TIMEOUT_MS=30000,
            NAVIGATION_SETTLE_MS=3000,
            ACTION_SETTLE_MS=500,
            CLICK_RESULT_WAIT_MS=4000,
            VISIBILITY_TIMEOUT_MS=2000,
        )

    def test_playwright_default_timeouts_and_strict_goto(self):
        page, context, browser, playwright = self._browser_stubs()
        ns = self._cookie_namespace(playwright)
        data = ns['get_cookies_via_browser']('https://kad.arbitr.ru/', None, 'UA', headless=True)

        self.assertEqual(data['cookies'], {'a': 'b'})
        page.set_default_timeout.assert_called_once_with(30000)
        context.set_default_timeout.assert_called_once_with(30000)
        self.assertEqual(page.goto.call_args.kwargs['timeout'], 30000)
        self.assertEqual(playwright.chromium.launch.call_args.kwargs['timeout'], 30000)
        browser.close.assert_called_once()
        context.close.assert_called_once()

    def test_playwright_timeout_defaults_are_strict(self):
        source = (ROOT / 'enricher/services/cookies.py').read_text()
        self.assertIn('context.set_default_timeout(PAGE_TIMEOUT_MS)', source)
        self.assertIn('page.set_default_timeout(PAGE_TIMEOUT_MS)', source)
        self.assertIn('PLAYWRIGHT_PAGE_TIMEOUT_MS", "30000"', source)
        self.assertIn('PLAYWRIGHT_LAUNCH_TIMEOUT_MS", "30000"', source)
        self.assertIn('page.goto(start_url, wait_until=', source)

    def test_playwright_browser_closed_on_critical_error(self):
        page, context, browser, playwright = self._browser_stubs()
        context.cookies.side_effect = RuntimeError('target closed')
        ns = self._cookie_namespace(playwright)
        data = ns['get_cookies_via_browser']('https://kad.arbitr.ru/', None, 'UA')

        self.assertEqual(data, {'cookies': {}, 'cookiesRaw': []})
        browser.close.assert_called_once()
        context.close.assert_called_once()

    # ---------- Обновление кук: жёсткий тайм-аут ----------

    def test_cookie_refresh_hard_timeout(self):
        release = threading.Event()
        started = threading.Event()

        def hanging_browser(**kwargs):
            started.set()
            release.wait(5)
            return {'cookies': {'session': 'x'}}

        class CookieRefreshTimeout(TimeoutError):
            pass

        class CookieRefreshError(RuntimeError):
            pass

        saved = Mock()
        guard = threading.Lock()
        ns = functions(
            'core/parser.py', {'refresh_cookies', '_refresh_worker'},
            ThreadPoolExecutor=ThreadPoolExecutor,
            concurrent=concurrent,
            _refresh_guard=guard,
            get_cookies_via_browser=hanging_browser,
            logger=logging.getLogger(__name__),
            CookieRefreshTimeout=CookieRefreshTimeout,
            CookieRefreshError=CookieRefreshError,
            save_cookies=saved,
            COOKIES_FILE='unused',
            COOKIE_REFRESH_TIMEOUT=60,
            START_URL='https://kad.arbitr.ru/',
            USER_AGENT='UA',
            platform=platform,
            PROXY_URL=None,
            parse_proxy_url=lambda proxy_url: None,
            redact_proxy_error=lambda error, proxy_url: str(error),
        )

        began = time.monotonic()
        with self.assertRaises(TimeoutError) as ctx:
            ns['refresh_cookies'](0.2)
        self.assertLess(time.monotonic() - began, 2.0)
        self.assertIsInstance(ctx.exception, CookieRefreshTimeout)
        self.assertTrue(started.wait(1))
        saved.assert_not_called()

        # Пока зависший поток жив, новые попытки отклоняются сразу,
        # а не порождают второй браузер.
        with self.assertRaises(CookieRefreshTimeout):
            ns['refresh_cookies'](5)

        release.set()
        self.assertTrue(guard.acquire(timeout=5))
        guard.release()

    def test_cookie_refresh_rejects_empty_cookies(self):
        class CookieRefreshTimeout(TimeoutError):
            pass

        class CookieRefreshError(RuntimeError):
            pass

        saved = Mock()
        ns = functions(
            'core/parser.py', {'refresh_cookies', '_refresh_worker'},
            ThreadPoolExecutor=ThreadPoolExecutor,
            concurrent=concurrent,
            _refresh_guard=threading.Lock(),
            get_cookies_via_browser=lambda **kwargs: {'cookies': {}, 'cookiesRaw': []},
            logger=logging.getLogger(__name__),
            CookieRefreshTimeout=CookieRefreshTimeout,
            CookieRefreshError=CookieRefreshError,
            save_cookies=saved,
            COOKIES_FILE='unused',
            COOKIE_REFRESH_TIMEOUT=60,
            START_URL='https://kad.arbitr.ru/',
            USER_AGENT='UA',
            platform=platform,
            PROXY_URL=None,
            parse_proxy_url=lambda proxy_url: None,
            redact_proxy_error=lambda error, proxy_url: str(error),
        )
        with self.assertRaises(CookieRefreshError):
            ns['refresh_cookies'](10)
        saved.assert_not_called()

    # ---------- Пагинация ----------

    def test_pagination_is_capped(self):
        clock = Mock()
        get_cases = Mock(return_value=([{'caseNumber': 'A'}], {}, {}, None, 999, 'num_case'))

        def refresh():  # куки валидны — обновление не требуется
            raise AssertionError('refresh_cookies не должен вызываться')

        ns = functions(
            'core/parser.py', {'fetch_cases_by_inn'},
            COOKIES_FILE='unused', datetime=datetime, timedelta=timedelta,
            ZoneInfo=ZoneInfo, logger=logging.getLogger(__name__),
            get_cases=get_cases, CookieExpiredError=RuntimeError,
            refresh_cookies=refresh, time=clock, random=random,
        )

        cases = ns['fetch_cases_by_inn']('7707083893', max_pages=3)

        self.assertEqual(len(cases), 3)
        self.assertEqual(get_cases.call_count, 3)  # 999 страниц не листаем

    def test_cookie_refresh_timeout_is_not_retried(self):
        get_cases = Mock(return_value=([], {}, {}, None, 1, ''))
        refresh = Mock(side_effect=TimeoutError('обновление кук не завершилось за 60 с'))
        ns = functions(
            'core/parser.py', {'fetch_cases_by_inn'},
            COOKIES_FILE='unused', datetime=datetime, timedelta=timedelta,
            ZoneInfo=ZoneInfo, logger=logging.getLogger(__name__),
            get_cases=get_cases, CookieExpiredError=RuntimeError,
            refresh_cookies=refresh, time=Mock(), random=random,
        )

        with self.assertRaises(TimeoutError):
            ns['fetch_cases_by_inn']('7707083893')

        self.assertEqual(refresh.call_count, 1)

    # ---------- Проверка клиентов ----------

    def test_checker_aborts_on_cookie_refresh_timeout(self):
        clock = Mock()
        fetch = Mock(side_effect=TimeoutError('куки не обновились за 60 с'))
        ns = functions(
            'core/checker.py', {'check_all_clients'}, Dict=dict, List=list,
            get_all_clients=lambda _: [dict(inn='1', name='A'), dict(inn='2', name='B')],
            fetch_cases_by_inn=fetch,
            case_exists=lambda *a: False,
            add_case=Mock(return_value=True),
            time=clock,
            random=random,
            logger=logging.getLogger(__name__),
            CookieExpiredError=RuntimeError,
        )

        results, errors = ns['check_all_clients'](1)

        self.assertEqual(results, {})
        self.assertEqual(fetch.call_count, 1)  # второго клиента уже не проверяем
        self.assertTrue(any('тайм-аут' in error for error in errors))

    def test_cmd_check_timeout_reports_and_releases_lock(self):
        gate = threading.Event()

        def hanging_check(user_id):
            gate.wait(30)  # «зависшая» проверка: снимаем вручную в конце теста
            return {}, []

        check_lock = asyncio.Lock()
        busy = threading.Event()
        ns = functions(
            'bot/handlers.py', {'cmd_check', '_check_in_thread'},
            Message=object,
            asyncio=asyncio,
            logger=logging.getLogger(__name__),
            _check_lock=check_lock,
            _check_busy=busy,
            CHECK_TIMEOUT_SECONDS=0.1,
            check_all_clients=hanging_check,
            html_value=lambda value: escape(str(value)),
            send_long_message=AsyncMock(),
        )
        message = SimpleNamespace(from_user=SimpleNamespace(id=1), answer=AsyncMock())

        def run(coro):
            # asyncio.run() при закрытии ждёт default executor, а он занят
            # «зависшей» проверкой — поэтому закрываем цикл без ожидания.
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(coro)
            finally:
                loop.close()

        run(ns['cmd_check'](message))

        answers = ' '.join(str(call.args[0]) for call in message.answer.call_args_list)
        self.assertIn('Проверка прервана из-за тайм-аута', answers)
        self.assertFalse(check_lock.locked())  # lock не утекает
        self.assertTrue(busy.is_set())  # поток ещё жив

        second = SimpleNamespace(from_user=SimpleNamespace(id=1), answer=AsyncMock())
        run(ns['cmd_check'](second))
        self.assertIn('уже выполняется', second.answer.call_args.args[0])

        gate.set()
        for _ in range(100):
            if not busy.is_set():
                break
            time.sleep(0.05)
        self.assertFalse(busy.is_set())
        self.assertFalse(check_lock.locked())

    def test_cmd_check_runs_in_thread_with_wait_for(self):
        tree = ast.parse((ROOT / 'bot/handlers.py').read_text())
        cmd_check = next(
            node for node in tree.body
            if isinstance(node, ast.AsyncFunctionDef) and node.name == 'cmd_check'
        )
        source = ast.unparse(cmd_check)
        self.assertIn('asyncio.wait_for', source)
        self.assertIn('asyncio.to_thread', source)
        self.assertIn('timeout=CHECK_TIMEOUT_SECONDS', source)

    # ---------- Запросы к сайту суда ----------

    def test_kad_requests_are_bounded_and_retried(self):
        source = (ROOT / 'enricher/services/cases.py').read_text()
        tree = ast.parse(source)
        fetch = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == 'fetch_cases_html'
        )
        posts = [
            node for node in ast.walk(fetch)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == 'post'
        ]
        self.assertTrue(posts, 'fetch_cases_html должен отправлять запрос через сессию')
        self.assertTrue(any(kw.arg == 'timeout' for kw in posts[0].keywords))
        self.assertIn('Retry(', source)
        self.assertIn('status_forcelist=RETRY_STATUSES', source)
        self.assertIn('allowed_methods=frozenset({"GET", "POST", "HEAD"})', source)


if __name__ == '__main__':
    unittest.main()
