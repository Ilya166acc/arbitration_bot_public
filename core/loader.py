import pandas as pd
from config import CLIENTS_CSV
from database.db import add_client, get_all_clients


def load_clients_from_csv(user_id: int) -> dict:
    try:
        df = pd.read_csv(CLIENTS_CSV, dtype={"inn": str})
    except FileNotFoundError:
        return {"error": f"Файл не найден: {CLIENTS_CSV}"}
    except Exception as e:
        return {"error": f"Ошибка чтения CSV: {e}"}

    added = 0
    skipped = 0
    errors = []

    for _, row in df.iterrows():
        inn = str(row.get("inn", "")).strip()
        name = str(row.get("name", "")).strip()

        if not inn.isdigit() or len(inn) not in (10, 12):
            errors.append(f"Неверный ИНН: {inn} ({name})")
            continue

        if not name:
            errors.append(f"Пустое название для ИНН {inn}")
            continue

        if add_client(user_id, inn, name):
            added += 1
        else:
            skipped += 1

    return {
        "added": added,
        "skipped": skipped,
        "errors": errors,
        "total_in_db": len(get_all_clients(user_id))
    }


if __name__ == "__main__":
    result = load_clients_from_csv()
    print("📊 Результат загрузки:")
    for key, value in result.items():
        print(f"  {key}: {value}")