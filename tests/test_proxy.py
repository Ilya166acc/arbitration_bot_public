"""Offline tests for PROXY_URL parsing and wiring; no network access required."""
import ast
import importlib.util
import logging
import multiprocessing
from multiprocessing.connection import wait as wait_connections
import os
from pathlib import Path
import signal
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from core.proxy import parse_proxy_url, redact_proxy_error

ROOT = Path(__file__).resolve().parents[1]
HTTP_PROXY = "http://account-zone-cis-region-ru-session-123-sessTime-30:p%40ss%3Aword@ru.proxy.rucaptcha.com:2334"
SOCKS_PROXY = "socks5://account-zone-cis-region-ru-session-123-sessTime-30:p%40ss%3Aword@ru.proxy.rucaptcha.com:2333"


def functions(path, names, **namespace):
    tree = ast.parse((ROOT / path).read_text())
    tree.body = [
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names
    ]
    for node in tree.body:
        node.decorator_list = []
    exec(compile(tree, str(path), "exec"), namespace)
    return namespace


class ProxySettingsTests(unittest.TestCase):
    def test_http_proxy_formats(self):
        settings = parse_proxy_url(HTTP_PROXY)
        self.assertIsNotNone(settings)
        self.assertEqual(settings.as_playwright(), {
            "server": "http://ru.proxy.rucaptcha.com:2334",
            "username": "account-zone-cis-region-ru-session-123-sessTime-30",
            "password": "p@ss:word",
        })
        self.assertEqual(settings.as_requests(), {"http": HTTP_PROXY, "https": HTTP_PROXY})

    def test_socks_proxy_formats(self):
        settings = parse_proxy_url(SOCKS_PROXY)
        self.assertIsNotNone(settings)
        self.assertEqual(settings.as_playwright()["server"], "socks5://ru.proxy.rucaptcha.com:2333")
        self.assertEqual(settings.as_requests(), {"http": SOCKS_PROXY, "https": SOCKS_PROXY})
        remote_dns = parse_proxy_url(SOCKS_PROXY.replace("socks5://", "socks5h://", 1))
        self.assertEqual(remote_dns.as_playwright()["server"], "socks5://ru.proxy.rucaptcha.com:2333")
        self.assertTrue(remote_dns.as_requests()["https"].startswith("socks5h://"))

    def test_missing_or_invalid_proxy_falls_back_without_leaking_secrets(self):
        for value in (None, "", "   ", "ftp://user:secret@host:2334", "http://user:secret@host",
                      "http://user@host:2334", "http://user:secret@host:99999",
                      "http://user:secret@host:2334/path"):
            with self.subTest(value=value), self.assertLogs("core.proxy", level="ERROR") as logs:
                self.assertIsNone(parse_proxy_url(value))
            self.assertNotIn("secret", "\n".join(logs.output))

    def test_redacts_credentials_from_errors(self):
        settings = parse_proxy_url(HTTP_PROXY)
        message = f"failed {settings.url} for {settings.username} / {settings.password}"
        safe = redact_proxy_error(RuntimeError(message), settings.url)
        self.assertNotIn(settings.username, safe)
        self.assertNotIn(settings.password, safe)
        self.assertNotIn(settings.url, safe)
        self.assertIn("***", safe)

    def test_config_reads_proxy_url_from_environment(self):
        spec = importlib.util.spec_from_file_location("proxy_config_under_test", ROOT / "config.py")
        module = importlib.util.module_from_spec(spec)
        fake_dotenv = SimpleNamespace(load_dotenv=lambda: None)
        with patch.dict(sys.modules, {"dotenv": fake_dotenv}), patch.dict(os.environ, {"PROXY_URL": HTTP_PROXY}, clear=True):
            spec.loader.exec_module(module)
        self.assertEqual(module.PROXY_URL, HTTP_PROXY)


class CasesProxyWiringTests(unittest.TestCase):
    class RequestException(Exception):
        pass

    class HTTPError(RequestException):
        pass

    class ProxyError(RequestException):
        pass

    class Timeout(RequestException):
        pass

    class ConnectionError(RequestException):
        pass

    class ProxyConnectionError(RuntimeError):
        pass

    class CookieExpiredError(Exception):
        pass

    def setUp(self):
        self.requests = SimpleNamespace(
            HTTPError=self.HTTPError, ProxyError=self.ProxyError, Timeout=self.Timeout,
            ConnectionError=self.ConnectionError, RequestException=self.RequestException,
        )
        self.session = Mock()
        self.response = Mock(status_code=200, text="ok")
        self.response.raise_for_status.return_value = None
        self.session.post.return_value = self.response
        self.logger = logging.getLogger("cases.proxy.test")

    def namespace(self, proxy_url, ssl_verify=False):
        return functions(
            "enricher/services/cases.py", {"build_cases_request", "fetch_cases_html"},
            logger=self.logger, PROXY_URL=proxy_url, parse_proxy_url=parse_proxy_url,
            redact_proxy_error=redact_proxy_error, session=self.session,
            DEFAULT_TIMEOUT=(10, 45), SSL_VERIFY=ssl_verify,
            ProxyConnectionError=self.ProxyConnectionError,
            CookieExpiredError=self.CookieExpiredError, requests=self.requests,
        )

    def test_build_cases_request_returns_proxies(self):
        ns = self.namespace(HTTP_PROXY)
        _, _, _, proxies = ns["build_cases_request"](1, 25)
        self.assertEqual(proxies, {"http": HTTP_PROXY, "https": HTTP_PROXY})
        ns = self.namespace("invalid")
        self.assertIsNone(ns["build_cases_request"](1, 25)[3])

    def test_fetch_passes_proxies_to_session_post(self):
        ns = self.namespace(HTTP_PROXY)
        proxies = {"http": HTTP_PROXY, "https": HTTP_PROXY}
        html = ns["fetch_cases_html"]("https://kad.arbitr.ru/Kad/SearchInstances", {}, {}, {"Page": 1}, proxies)
        self.assertEqual(html, "ok")
        self.assertIs(self.session.post.call_args.kwargs["proxies"], proxies)
        self.assertEqual(self.session.post.call_args.kwargs["timeout"], (10, 45))
        self.assertIs(self.session.post.call_args.kwargs["verify"], False)

    def test_fetch_supports_custom_ca_bundle_path(self):
        ca_path = "/app/certs/ca-bundle.pem"
        ns = self.namespace(HTTP_PROXY, ssl_verify=ca_path)
        proxies = {"http": HTTP_PROXY, "https": HTTP_PROXY}
        ns["fetch_cases_html"]("https://kad.arbitr.ru/Kad/SearchInstances", {}, {}, {"Page": 1}, proxies)
        self.assertEqual(self.session.post.call_args.kwargs["verify"], ca_path)

    def test_proxy_failures_are_raised_and_redacted(self):
        ns = self.namespace(HTTP_PROXY)
        proxies = {"https": HTTP_PROXY}
        settings = parse_proxy_url(HTTP_PROXY)
        for error_class in (self.ProxyError, self.Timeout, self.ConnectionError):
            with self.subTest(error_class=error_class.__name__):
                self.session.post.side_effect = error_class(
                    f"proxy failed {settings.url} {settings.username} {settings.password}"
                )
                with self.assertLogs(self.logger, level="ERROR") as logs:
                    with self.assertRaises(self.ProxyConnectionError) as raised:
                        ns["fetch_cases_html"]("https://kad.arbitr.ru/", {}, {}, {}, proxies)
                self.assertNotIn(settings.password, str(raised.exception))
                self.assertNotIn(settings.password, "\n".join(logs.output))
        self.session.post.side_effect = None

    def test_http_407_is_proxy_error_and_451_remains_cookie_error(self):
        ns = self.namespace(HTTP_PROXY)
        proxies = {"https": HTTP_PROXY}
        error = self.HTTPError("407 Proxy Authentication Required")
        error.response = SimpleNamespace(status_code=407)
        self.response.raise_for_status.side_effect = error
        with self.assertRaises(self.ProxyConnectionError):
            ns["fetch_cases_html"]("https://kad.arbitr.ru/", {}, {}, {}, proxies)
        error.response = SimpleNamespace(status_code=451)
        with self.assertRaises(self.CookieExpiredError):
            ns["fetch_cases_html"]("https://kad.arbitr.ru/", {}, {}, {}, proxies)


class ParserProxyWiringTests(unittest.TestCase):
    def namespace(self, proxy_url, browser):
        class CookieRefreshError(RuntimeError):
            pass

        class CookieRefreshTimeout(TimeoutError):
            pass

        self.CookieRefreshError = CookieRefreshError
        self.saved = Mock()
        mp_proxy = SimpleNamespace(
            get_context=lambda _method: multiprocessing.get_context("fork")
        )
        return functions(
            "core/parser.py", {"refresh_cookies", "_refresh_worker", "_terminate_process"},
            multiprocessing=mp_proxy,
            wait_connections=wait_connections,
            os=os,
            signal=signal,
            logging=logging,
            PROCESS_TERMINATE_GRACE_SECONDS=0.5,
            _refresh_guard=threading.Lock(),
            get_cookies_via_browser=browser, logger=logging.getLogger("parser.proxy.test"),
            CookieRefreshTimeout=CookieRefreshTimeout, CookieRefreshError=CookieRefreshError,
            save_cookies=self.saved, COOKIES_FILE="unused", COOKIE_REFRESH_TIMEOUT=60,
            START_URL="https://kad.arbitr.ru/", USER_AGENT="UA",
            platform=SimpleNamespace(system=lambda: "Linux"),
            PROXY_URL=proxy_url, parse_proxy_url=parse_proxy_url,
            redact_proxy_error=redact_proxy_error,
        )

    def test_refresh_passes_proxy_to_playwright(self):
        def browser(**kwargs):
            return {"cookies": {"session": "x"}, "seen_proxy": kwargs["proxy"]}

        ns = self.namespace(HTTP_PROXY, browser)
        ns["refresh_cookies"](5)
        self.assertEqual(self.saved.call_args.args[1]["seen_proxy"], {
            "server": "http://ru.proxy.rucaptcha.com:2334",
            "username": "account-zone-cis-region-ru-session-123-sessTime-30",
            "password": "p@ss:word",
        })

    def test_invalid_proxy_falls_back_to_direct_browser(self):
        def browser(**kwargs):
            return {"cookies": {"session": "x"}, "seen_proxy": kwargs["proxy"]}

        ns = self.namespace("not-a-proxy-url", browser)
        ns["refresh_cookies"](5)
        self.assertIsNone(self.saved.call_args.args[1]["seen_proxy"])

    def test_browser_proxy_failure_is_raised_and_redacted(self):
        settings = parse_proxy_url(HTTP_PROXY)

        def browser(**kwargs):
            raise RuntimeError(f"proxy failed {settings.url} {settings.password}")

        ns = self.namespace(HTTP_PROXY, browser)
        with self.assertRaises(self.CookieRefreshError) as raised:
            ns["refresh_cookies"](5)
        self.assertNotIn(settings.password, str(raised.exception))
        self.assertIn("ru.proxy.rucaptcha.com:2334", str(raised.exception))
        self.assertTrue(ns["_refresh_guard"].acquire(timeout=1))
        ns["_refresh_guard"].release()


class BrowserProxyErrorTests(unittest.TestCase):
    def namespace(self, page):
        context = Mock()
        context.new_page.return_value = page
        context.cookies.return_value = [{"name": "a", "value": "b"}]
        browser = Mock()
        browser.new_context.return_value = context
        playwright = Mock()
        playwright.chromium.launch.return_value = browser

        class PlaywrightContext:
            def __enter__(self):
                return playwright

            def __exit__(self, *exc_info):
                return False

        ns = functions(
            "enricher/services/cookies.py", {"get_cookies_via_browser"},
            sync_playwright=lambda: PlaywrightContext(), logger=logging.getLogger("cookies.proxy.test"),
            PAGE_TIMEOUT_MS=30000, BROWSER_LAUNCH_TIMEOUT_MS=30000,
            NAVIGATION_SETTLE_MS=0, ACTION_SETTLE_MS=0, CLICK_RESULT_WAIT_MS=0,
            VISIBILITY_TIMEOUT_MS=0,
        )
        return ns, browser, context

    def test_navigation_proxy_error_propagates_and_closes_browser(self):
        page = Mock()
        page.goto.side_effect = RuntimeError("proxy authentication failed")
        ns, browser, context = self.namespace(page)
        with self.assertRaises(RuntimeError):
            ns["get_cookies_via_browser"](
                "https://kad.arbitr.ru/", {"server": "http://ru.proxy.rucaptcha.com:2334"}, "UA", headless=True
            )
        browser.close.assert_called_once()
        context.close.assert_called_once()

    def test_http_407_navigation_propagates(self):
        page = Mock()
        page.goto.return_value = SimpleNamespace(status=407)
        ns, browser, context = self.namespace(page)
        with self.assertRaises(RuntimeError):
            ns["get_cookies_via_browser"](
                "https://kad.arbitr.ru/", {"server": "http://ru.proxy.rucaptcha.com:2334"}, "UA", headless=True
            )
        browser.close.assert_called_once()
        context.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
