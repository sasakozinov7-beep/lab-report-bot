FROM python:3.11-slim

WORKDIR /app

# Установка системных зависимостей, LibreOffice для экспорта в PDF и шрифтов
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libreoffice-writer \
    fonts-dejavu \
    fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

# Копируем список зависимостей
COPY requirements.txt .

# Устанавливаем зависимости
RUN pip install --no-cache-dir -r requirements.txt

# Копируем код приложения
COPY . .

# Создаем папку для сессий
RUN mkdir -p sessions

# Запуск бота
CMD ["python", "bot.py"]
