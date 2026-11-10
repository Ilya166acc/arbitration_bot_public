"""
Тест поиска дел по ИНН.
Запуск: py enricher/test_inn.py 7712345678
"""
import sys
import json
import logging
from services.cases import get_cases

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s: %(message)s'
)

if len(sys.argv) < 2:
    print("Использование: py enricher/test_inn.py <ИНН>")
    sys.exit(1)

inn = sys.argv[1].strip()
print(f"\n🔍 Ищем дела по ИНН: {inn}\n")

rows, _, _, _, pages = get_cases(
    cookies_file="cookies.json",
    page=1,
    count=25,
    courts=None,       # все суды
    sides=[inn],       # ищем по ИНН в поле «Стороны»
)

print(f"\n📊 Найдено дел: {len(rows)}. Всего страниц: {pages}\n")
print(json.dumps(rows[:5], ensure_ascii=False, indent=2))  # показываем первые 5