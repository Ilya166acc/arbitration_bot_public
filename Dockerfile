# Используем официальный образ Python
FROM python:3.11-slim

# Устанавливаем системные зависимости, необходимые для Playwright, 
# А ТАКЖЕ curl и ca-certificates для скачивания сертификата Минцифры
RUN apt-get update && apt-get install -y \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libxkbcommon0 libxcomposite1 \
    libxdamage1 libxfixes3 libxrandr2 libgbm1 libasound2 \
    curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Скачиваем сертификат Минцифры и добавляем его в системное хранилище
RUN curl -fsSL https://gu-st.ru/content/lending/russian_trusted_root_ca_pem.crt -o /usr/local/share/ca-certificates/russian_trusted_root_ca.crt \
    && update-ca-certificates

# Устанавливаем рабочую директорию
WORKDIR /app

# Копируем файлы проекта
COPY . /app

# Устанавливаем Python-зависимости
RUN pip install --no-cache-dir -r requirements.txt

# Устанавливаем браузер Chromium для Playwright
RUN playwright install chromium

# Указываем путь к сертификату для вашего кода (переменная из PR агента)
ENV KAD_CA_BUNDLE=/usr/local/share/ca-certificates/russian_trusted_root_ca.crt

# Команда запуска бота
CMD ["python", "main.py"]