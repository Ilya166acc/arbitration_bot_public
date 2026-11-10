import logging
from typing import List, Dict

from database.db import get_all_clients, case_exists, add_case
from core.parser import fetch_cases_by_inn

logger = logging.getLogger(__name__)


def check_all_clients(user_id: int) -> tuple[Dict[str, List[Dict]], List[str]]:
    """
    Проверяет всех клиентов пользователя.
    Возвращает словарь с новыми делами и список ошибок.
    """
    clients = get_all_clients(user_id)
    result = {}
    errors = []

    for client in clients:
        inn = client["inn"]
        name = client["name"]
        days_back = client.get("days_back", 30)
        logger.info(f"🔍 Проверяю клиента: {name} (ИНН {inn}, за {days_back} дн.)")

        try:
            cases = fetch_cases_by_inn(inn, days_back=days_back)
        except Exception as e:
            logger.error(f"Ошибка при проверке {inn}: {e}")
            errors.append(f"Ошибка по клиенту {name} (ИНН {inn}): {e}")
            
            # Если это ошибка куки, прерываем проверку остальных клиентов
            if "HTTP 451" in str(e) or "CookieExpiredError" in str(e):
                logger.error("❌ Куки не удалось обновить. Прерываем проверку всех клиентов.")
                errors.append("⚠️ Проверка прервана: не удалось обновить куки (сайт блокирует сервер).")
                break 
                
            continue

        new_cases = []
        for case in cases:
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
                "court": case.get("court"),
                "plaintiff": ", ".join(case.get("plaintiffs", [])),
                "defendant": ", ".join(case.get("respondents", [])),
                "claim_amount": None,
                "receipt_date": case.get("date"),
                "url": case.get("caseUrl"),
                "ai_summary": None,
            }

            # Сохраняем в базу и добавляем в список
            if add_case(user_id, case_data):
                new_cases.append(case_data)
                logger.info(f"  ➕ Новое дело: {case_number}")

        if new_cases:
            result[name] = new_cases

    return result, errors