import logging
import random
import time
from typing import List, Dict

from database.db import get_all_clients, case_exists, add_case
from core.parser import fetch_cases_by_inn, CookieExpiredError

logger = logging.getLogger(__name__)

# Предохранитель от «бесконечной» проверки: даже если в базе оказались
# тысячи клиентов, один запуск не должен идти часами.
def check_all_clients(user_id: int, max_clients: int = 200) -> tuple[Dict[str, List[Dict]], List[str]]:
    """
    Проверяет всех клиентов пользователя.
    Возвращает словарь с новыми делами и список ошибок.

    Функция синхронная и всегда вызывается из рабочего потока
    (asyncio.to_thread), поэтому блокирующие запросы не тормозят event loop.
    """
    clients = get_all_clients(user_id)
    result = {}
    errors = []

    for index, client in enumerate(clients):
        if index >= max_clients:
            logger.warning(
                "Достигнут лимит клиентов на один запуск (%d) — остальные пропущены.",
                max_clients,
            )
            errors.append(
                f"⚠️ Проверены не все клиенты: лимит {max_clients} за один запуск."
            )
            break

        if index:
            time.sleep(random.uniform(2.0, 4.0))

        inn = str(client.get("inn") or "").strip()
        name = client.get("name") or inn or "—"
        if not inn:
            logger.warning("Пропускаю запись без ИНН: %s", name)
            errors.append(f"Пропущен клиент без ИНН: {name}")
            continue

        try:
            days_back = int(client.get("days_back") or 30)
        except (TypeError, ValueError):
            days_back = 30
        # Период: не меньше одного дня и не больше года.
        days_back = min(max(days_back, 1), 365)

        logger.info(f"🔍 Проверяю клиента: {name} (ИНН {inn}, за {days_back} дн.)")

        try:
            cases = fetch_cases_by_inn(inn, days_back=days_back)
        except Exception as e:
            logger.error(f"Ошибка при проверке {inn}: {e}")
            errors.append(f"Ошибка по клиенту {name} (ИНН {inn}): {e}")

            # Если это ошибка куки или тайм-аут браузера, прерываем проверку
            # остальных клиентов: иначе каждый следующий клиент снова упрётся
            # в ту же проблему и проверка будет идти часами.
            if isinstance(e, CookieExpiredError) or "HTTP 451" in str(e):
                logger.error("❌ Куки не удалось обновить. Прерываем проверку всех клиентов.")
                errors.append("⚠️ Проверка прервана: не удалось обновить куки (сайт блокирует сервер).")
                break

            if isinstance(e, TimeoutError):
                logger.error("⏱ Обновление кук не уложилось в тайм-аут. Прерываем проверку.")
                errors.append("⚠️ Проверка прервана: обновление кук не уложилось в тайм-аут.")
                break

            continue

        new_cases = []
        for case in cases:
            if not isinstance(case, dict):
                continue

            case_number = case.get("caseNumber")
            if not case_number:
                continue

            # Если дело уже есть в базе — пропускаем
            if case_exists(user_id, case_number):
                continue

            # Собираем данные дела
            case_data = {
                "case_number": case_number,
                "client_inn": inn,
                "court": case.get("court") or "—",
                "plaintiff": ", ".join(case.get("plaintiffs") or []),
                "defendant": ", ".join(case.get("respondents") or []),
                "claim_amount": None,
                "receipt_date": case.get("date") or "—",
                "url": case.get("caseUrl"),
                "ai_summary": None,
            }

            # Сохраняем в базу и добавляем в список
            if add_case(user_id, case_data):
                new_cases.append(case_data)
                logger.info(f"  ➕ Новое дело: {case_number}")

        if new_cases:
            # setdefault: у двух клиентов может совпадать название,
            # раньше вторая запись затирала результаты первой.
            result.setdefault(name, []).extend(new_cases)

    return result, errors
