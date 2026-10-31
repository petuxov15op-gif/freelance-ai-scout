FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY pyproject.toml ./
COPY scout ./scout
COPY migrations ./migrations
COPY alembic.ini ./
RUN pip install --no-cache-dir .
RUN useradd -m scout && mkdir -p /app/data && chown -R scout:scout /app
USER scout
CMD ["python", "-m", "scout.main"]
