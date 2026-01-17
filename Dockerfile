# Используем образ с Python и Playwright
FROM mcr.microsoft.com/playwright/python:v1.57.0-jammy

# Рабочая папка
WORKDIR /app

# Копируем файлы
COPY . .

# Устанавливаем библиотеки Python
# (Playwright и его браузеры уже есть в системе, их качать не надо)
RUN pip install --no-cache-dir aiogram google-genai aiosqlite aiohttp playwright

# Команда запуска

CMD ["python", "main.py"]

