# База: чистый и стабильный Python
FROM python:3.11-slim

# Настройки Python
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Сначала зависимости (кэшируется)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Потом весь проект
COPY . .

# Запуск бота
CMD ["python", "Lib/main.py"]