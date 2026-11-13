import os
from dotenv import load_dotenv

# Загружаем переменные из .env
load_dotenv()

# === Секреты ===
BOT_TOKEN = os.getenv("BOT_TOKEN")
GIGACHAT_CREDENTIALS = os.getenv("GIGACHAT_CREDENTIALS")
# Прокси для kad.arbitr.ru (RuCaptcha residential и совместимые).
# Пример: http://USER:PASSWORD@ru.proxy.rucaptcha.com:2334
# Для SOCKS5: socks5://USER:PASSWORD@ru.proxy.rucaptcha.com:2333
# Если переменная не задана или некорректна, используется прямое соединение.
PROXY_URL = os.getenv("PROXY_URL")
# Список ID пользователей, которым разрешен доступ
ALLOWED_USER_IDS = [int(x.strip()) for x in os.getenv("ALLOWED_USER_IDS", "").split(",") if x.strip()]

# === Пути к файлам ===
CLIENTS_CSV = "data/clients.csv"
DATABASE_PATH = "data/bot.db"
COOKIES_PATH = "cookies.json"

# === Расписание проверок ===
# Интервал в днях (7 = раз в неделю, 3 = раз в три дня и т.д.)
CHECK_INTERVAL_DAYS = int(os.getenv("CHECK_INTERVAL_DAYS", 7))

# День недели и время запуска (для APScheduler)
# mon, tue, wed, thu, fri, sat, sun
CHECK_DAY_OF_WEEK = "mon"
CHECK_HOUR = 9
CHECK_MINUTE = 0

# === GigaChat ===
GIGACHAT_MODEL = "GigaChat"  # Lite-версия, бесплатная
GIGACHAT_TEMPERATURE = 0.1    # Чем ниже — тем строже и предсказуемее

# === Логирование ===
LOG_LEVEL = "INFO"