FROM node:22-bookworm-slim@sha256:6c74791e557ce11fc957704f6d4fe134a7bc8d6f5ca4403205b2966bd488f6b3

ARG CODEX_VERSION=0.145.0
ARG UV_VERSION=0.11.9
ARG NEWSBOT_UID=10001
ARG NEWSBOT_GID=10001

ENV DEBIAN_FRONTEND=noninteractive \
    PATH="/opt/newsbot-venv/bin:/usr/local/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    NEWSBOT_APP_DIR=/app \
    NEWSBOT_PYTHON_BIN=/opt/newsbot-venv/bin/python \
    NEWSBOT_CODEX_BIN=/usr/local/bin/codex \
    NEWSBOT_CRON_STATE_DIR=/app/data/cron \
    UV_PROJECT_ENVIRONMENT=/opt/newsbot-venv

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        ca-certificates \
        git \
        python3 \
        python3-pip \
        python3-venv \
        ripgrep \
        tzdata \
        util-linux \
    && npm install --global "@openai/codex@${CODEX_VERSION}" \
    && python3 -m venv /opt/newsbot-venv \
    && /opt/newsbot-venv/bin/python -m pip install --no-cache-dir --upgrade pip \
    && /opt/newsbot-venv/bin/python -m pip install --no-cache-dir "uv==${UV_VERSION}" \
    && rm -rf \
        /root/.cache \
        /root/.npm \
        /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml uv.lock README.md LICENSE ./
RUN /opt/newsbot-venv/bin/uv sync \
        --frozen \
        --inexact \
        --no-cache \
        --no-dev \
        --extra docker \
        --no-install-project

COPY newsbot ./newsbot
RUN /opt/newsbot-venv/bin/uv sync \
        --frozen \
        --inexact \
        --no-cache \
        --no-dev \
        --extra docker

COPY config ./config
COPY prompts ./prompts
COPY scripts ./scripts

RUN groupadd --gid "${NEWSBOT_GID}" newsbot \
    && useradd --uid "${NEWSBOT_UID}" --gid "${NEWSBOT_GID}" \
        --create-home --home-dir /home/newsbot --shell /bin/bash newsbot \
    && /opt/newsbot-venv/bin/python -m pip uninstall --yes \
        pip \
        setuptools \
        uv \
        wheel \
    && apt-get purge --yes \
        python3-pip \
        python3-pip-whl \
        python3-pkg-resources \
        python3-setuptools \
        python3-setuptools-whl \
        python3-venv \
        python3-wheel \
        python3.11-venv \
    && apt-get autoremove --yes \
    && rm -rf \
        /opt/yarn-v1.22.22 \
        /root/.cache \
        /root/.npm \
        /usr/local/bin/corepack \
        /usr/local/bin/npm \
        /usr/local/bin/npx \
        /usr/local/bin/pnpm \
        /usr/local/bin/pnpx \
        /usr/local/bin/yarn \
        /usr/local/bin/yarnpkg \
        /usr/local/lib/node_modules/corepack \
        /usr/local/lib/node_modules/npm \
        /var/lib/apt/lists/* \
    && mkdir -p \
        /app/data/cron \
        /app/logs \
        /app/payloads \
        /app/reports \
        /home/newsbot/.codex \
    && chown -R newsbot:newsbot \
        /app \
        /home/newsbot \
        /opt/newsbot-venv

USER newsbot

CMD ["python", "-m", "newsbot.cli", "run-discord-bot"]
