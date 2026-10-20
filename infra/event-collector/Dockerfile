FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN pip install "docker==7.1.0" "psycopg[binary]==3.2.3"
COPY collector.py /app/collector.py
RUN useradd --uid 10001 --no-create-home collector
USER collector
CMD ["python", "/app/collector.py"]
