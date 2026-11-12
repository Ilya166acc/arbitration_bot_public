# arbitration_bot_public

Telegram-бот для мониторинга арбитражных дел клиентов по ИНН через [kad.arbitr.ru](https://kad.arbitr.ru/).
Проверки идут по расписанию или вручную командой `/check`; найденные дела сохраняются в SQLite.

## Команды бота

| Команда | Назначение |
| --- | --- |
| `/start`, `/help` | Приветствие и справка |
| `/clients` | Список отслеживаемых клиентов |
| `/add <ИНН> [дни]` | Добавить клиента (ИНН проверяется по контрольной сумме) |
| `/del <ИНН>` | Удалить клиента и его дела |
| `/load` | Загрузить клиентов из `data/clients.csv` |
| `/check` | Запустить проверку сейчас |
| `/report` | Отчёт по сохранённым делам |

## Тайм-ауты и защита от зависаний

Playwright синхронный и не умеет прерывать собственные зависшие вызовы, поэтому каждый
внешний шаг ограничен по времени на своём уровне:

| Что | Где | Значение |
| --- | --- | --- |
| `page`/`context` default timeout, `page.goto`, `launch` | `enricher/services/cookies.py` | 30 с (`PLAYWRIGHT_PAGE_TIMEOUT_MS`, `PLAYWRIGHT_LAUNCH_TIMEOUT_MS`) |
| Обновление кук целиком (`ThreadPoolExecutor` + `future.result(timeout=...)`) | `core/parser.py` | 60 с (`COOKIE_REFRESH_TIMEOUT`) |
| Одна проверка всех клиентов (`asyncio.wait_for`) | `bot/handlers.py` | 300 с (`CHECK_TIMEOUT_SECONDS`) |
| Запросы к сайту (`requests`, connect/read) | `enricher/services/cases.py` | 10 / 45 с (`KAD_CONNECT_TIMEOUT`, `KAD_READ_TIMEOUT`) + 3 повтора |
| Пагинация по одному ИНН | `core/parser.py` | не более 50 страниц |
| Клиентов за один запуск | `core/checker.py` | не более 200 |

Если обновление кук не уложилось в 60 секунд, поднимается `CookieRefreshTimeout`
(наследник `TimeoutError`), проверка останавливается, пользователь получает отчёт об ошибке,
а бот продолжает отвечать на другие команды. Если проверка не уложилась в 300 секунд,
`/check` отвечает: «Проверка прервана из-за тайм-аута».

## Переменные окружения

`BOT_TOKEN`, `ALLOWED_USER_IDS`, `CHECK_INTERVAL_DAYS` — базовая конфигурация.
Тайм-ауты из таблицы выше можно переопределить одноимёнными переменными окружения.

## Запуск

```bash
cp .env.example .env   # BOT_TOKEN=...
pip install -r requirements.txt
playwright install chromium
python main.py
```

Docker: `docker build -t arbitration-bot . && docker run --env-file .env arbitration-bot`.

## Тесты

```bash
python -m unittest discover -s tests -v
```

Тесты офлайновые: Telegram, браузер и kad.arbitr.ru не нужны.
