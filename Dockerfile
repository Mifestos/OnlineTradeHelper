FROM python:3.12-slim

# Установка системных зависимостей
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Установка Poetry
RUN pip install --no-cache-dir poetry

# Создаём непривилегированного пользователя
RUN useradd -m -u 1000 -s /bin/bash appuser

WORKDIR /app

# Копируем файлы зависимостей и сразу меняем владельца
COPY --chown=appuser:appuser pyproject.toml poetry.lock* ./

# Poetry ставит пакеты в системный site-packages.
# Это делаем от root, чтобы не было проблем с правами на /usr/local/lib.
RUN poetry config virtualenvs.create false \
    && poetry install --no-interaction --no-ansi --no-root

# Копируем код проекта уже с правами appuser
COPY --chown=appuser:appuser . .

ENV PYTHONPATH="."

# Переключаемся на непривилегированного пользователя
USER appuser

CMD ["python"]