# Сборка MAX-версии бота «Хороший полицейский».
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Шрифт с кириллицей для карточек-иллюстраций: в slim-образе шрифтов
# нет, и Pillow рисовал бы вместо русских букв пустые квадраты.
RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

RUN mkdir -p storage/images logs

CMD ["python", "main.py"]
