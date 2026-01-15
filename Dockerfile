# Используем легковесный образ Python 3.12
FROM python:3.12-slim

# Устанавливаем системные зависимости (нужны для некоторых python пакетов)
RUN apt-get update && apt-get install -y gcc && rm -rf /var/lib/apt/lists/*

# Устанавливаем рабочую директорию
WORKDIR /app

# Копируем requirements и устанавливаем зависимости
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем весь проект в контейнер
COPY . .

# Делаем файл .env доступным (В реальном проде лучше передавать как ENV ARG)
# Здесь просто копируем файл, если он есть
RUN if [ -f .env ]; then echo "ENV found"; else echo "WARNING: .env file not found"; fi

# Открываем порт 8000
EXPOSE 8000

# Команда запуска приложения
# Запускаем uvicorn, который автоматически подхватит .env
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]