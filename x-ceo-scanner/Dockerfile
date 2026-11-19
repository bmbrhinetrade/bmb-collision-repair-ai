# ceoscan - real-time X (Twitter) CEO post scanner. ALERTS ONLY: never trades, never touches a brokerage.
#
#   docker build -t ceoscan .                     # or: docker compose build
#   docker compose run --rm ceoscan verify --write
#   docker compose up -d
#
# Secrets are NOT baked into the image: they come from .env at runtime (docker-compose `env_file`).
FROM python:3.12-slim-bookworm

# UID/GID of the unprivileged runtime user. docker-compose.yml passes your host UID/GID
# (default 1000) so the container can write the bind-mounted ./data and ./config directories.
ARG CEOSCAN_UID=10001
ARG CEOSCAN_GID=10001

LABEL org.opencontainers.image.title="ceoscan" \
      org.opencontainers.image.description="Real-time X CEO post scanner that pushes market-moving posts to your phone. Alerts only - never trades." \
      org.opencontainers.image.version="1.0.0"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore \
    PYTHONPATH=/app/src

WORKDIR /app

# Non-root runtime user (never root: refuse UID/GID 0). Reuse an existing group/user with the
# requested id if the base image already has one (e.g. GID 100 "users").
RUN set -eu; \
    if [ "${CEOSCAN_UID}" = "0" ] || [ "${CEOSCAN_GID}" = "0" ]; then \
        echo "CEOSCAN_UID/CEOSCAN_GID must not be 0 - the container never runs as root" >&2; exit 1; \
    fi; \
    if ! getent group "${CEOSCAN_GID}" >/dev/null; then groupadd --gid "${CEOSCAN_GID}" ceoscan; fi; \
    if ! getent passwd "${CEOSCAN_UID}" >/dev/null; then \
        useradd --uid "${CEOSCAN_UID}" --gid "${CEOSCAN_GID}" --no-create-home --home-dir /app \
                --shell /usr/sbin/nologin --no-log-init ceoscan; \
    fi

# Dependencies first (pinned in requirements.txt) for better layer caching.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Application code is root-owned and read-only for the runtime user; config and data are
# writable (`verify --write` updates config/watchlist.yaml; SQLite lives in data/).
COPY src/ ./src/
COPY samples/ ./samples/
COPY --chown=${CEOSCAN_UID}:${CEOSCAN_GID} config/ ./config/
RUN python -m compileall -q /app/src \
    && mkdir -p /app/data \
    && chown -R "${CEOSCAN_UID}:${CEOSCAN_GID}" /app/data /app/config

USER ${CEOSCAN_UID}:${CEOSCAN_GID}

VOLUME ["/app/data"]
EXPOSE 8080

# 200 from the built-in health server = process is up and serving. (Use /health?strict=1 from an
# external monitor if you also want "X stream down / credits depleted" to count as unhealthy.)
# The empty ProxyHandler makes sure a configured HTTP(S)_PROXY is never used for localhost.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD ["python", "-c", "import sys, urllib.request as u; r = u.build_opener(u.ProxyHandler({})).open('http://127.0.0.1:8080/health', timeout=4); sys.exit(0 if r.status == 200 else 1)"]

STOPSIGNAL SIGTERM

# Lets you pass a ceoscan sub-command directly, e.g. `docker compose run --rm ceoscan verify --write`
# or `docker run --rm --env-file .env ceoscan cost`; anything else (like the default CMD) runs as-is.
# `exec` keeps Python as the signal-receiving process so SIGTERM triggers a graceful shutdown.
ENTRYPOINT ["/bin/sh", "-c", "if [ \"$#\" -eq 0 ]; then set -- run; fi; case \"$1\" in run|verify|rules|check-access|cost|replay|test-alert|-*) exec python -m ceoscan \"$@\" ;; *) exec \"$@\" ;; esac", "ceoscan"]
CMD ["python", "-m", "ceoscan", "run"]
