FROM python:3.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
RUN groupadd --system --gid 10001 foodops \
    && useradd --system --uid 10001 --gid foodops --home-dir /app foodops

COPY deploy/requirements.lock.txt /tmp/requirements.lock.txt
RUN pip install --no-cache-dir --require-hashes -r /tmp/requirements.lock.txt \
    && rm /tmp/requirements.lock.txt

COPY --chown=foodops:foodops . /app
RUN chmod 0755 /app/deploy/start.sh /app/deploy/init-db.sh \
    && DJANGO_DEBUG=0 \
    DJANGO_SECRET_KEY=build-only-abcdefghijklmnopqrstuvwxyz-0123456789-ABCDEFGHIJKLMNOPQRSTUVWXYZ \
    DJANGO_ALLOWED_HOSTS=localhost \
    DATABASE_URL=postgresql://build:build@localhost/build \
    APP_BASE_PATH=/estoque-foodops \
    python manage.py collectstatic --noinput --verbosity 0

USER foodops
EXPOSE 8000
CMD ["/app/deploy/start.sh"]
