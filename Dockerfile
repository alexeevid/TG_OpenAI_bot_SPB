FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# 1) Дотягиваем системные зависимости, которые чаще всего нужны Pillow, psycopg и т.д.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential gcc python3-dev libpq-dev \
    libjpeg62-turbo-dev zlib1g-dev libpng-dev libtiff5-dev libopenjp2-7 \
    poppler-utils \
 && rm -rf /var/lib/apt/lists/*

# 2) Обновляем pip/setuptools/wheel — без этого часто валится сборка бинарных колес
COPY requirements.txt .
RUN pip install --upgrade pip setuptools wheel \
 && pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "-m", "bot.main"]
