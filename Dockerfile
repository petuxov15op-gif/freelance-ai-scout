FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY pyproject.toml ./
COPY scout ./scout
COPY miniapp ./miniapp
COPY migrations ./migrations
COPY alembic.ini ./
RUN pip install --no-cache-dir .
RUN useradd -m scout && mkdir -p /app/data && chown -R scout:scout /app
ARG SCOUT_SOURCE_REVISION=unknown
LABEL org.opencontainers.image.title="Freelance AI Scout" \
      org.opencontainers.image.revision=$SCOUT_SOURCE_REVISION
USER scout
CMD ["python", "-m", "scout.main"]
