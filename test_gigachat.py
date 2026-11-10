import os
from dotenv import load_dotenv
from gigachat import GigaChat

# Загружаем ключ из .env
load_dotenv()
credentials = os.getenv("GIGACHAT_CREDENTIALS")

print("🔑 Ключ авторизации найден, пробую подключиться...")

try:
    # Инициализируем клиент
    # verify_ssl_certs=False нужен только для локальной разработки, так как могут потребоваться сертификаты Минцифры
    giga = GigaChat(credentials=credentials, verify_ssl_certs=False)

    # Отправляем простой тестовый запрос
    response = giga.chat("Скажи 'Привет, мир!' и больше ничего.")
    
    print("✅ Успех! Ответ от GigaChat:")
    print(response.choices[0].message.content)

except Exception as e:
    print(f"❌ Ошибка подключения: {e}")