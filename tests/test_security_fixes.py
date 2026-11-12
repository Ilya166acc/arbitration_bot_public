"""Offline regression tests; no Telegram, browser or KAD access required."""
import ast
import asyncio
from datetime import datetime, timedelta
from html import escape
import json
import logging
import os
from pathlib import Path
import random
import tempfile
from threading import Lock
import unittest
from unittest.mock import Mock
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


if __name__ == '__main__':
    unittest.main()
