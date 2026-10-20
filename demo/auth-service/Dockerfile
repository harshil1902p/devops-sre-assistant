FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
COPY config ./config

RUN useradd --uid 10001 --no-create-home appuser
USER appuser

EXPOSE 8080
CMD ["python", "-m", "app"]
