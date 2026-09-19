# AutoBill in a container (docs/deploy.md). Built by GitHub Actions for linux/amd64 and
# linux/arm64 and published as ghcr.io/yangmingunsw/autobill; nothing personal is inside:
# configuration, password and data all come from outside at run time.
#
#   docker compose up -d        # with compose.yaml: runs `autobill serve` (every 30 minutes)
#   docker run --rm -v "$PWD/data:/data" --env-file autobill.env <image> check-mailbox

FROM python:3.12-slim

# Chromium prints the standard statement to PDF (Debian's package exists for amd64 and
# arm64, unlike Google Chrome); Noto CJK renders the Chinese text instead of empty boxes.
RUN apt-get update \
    && apt-get install -y --no-install-recommends chromium fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.11.31 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PYTHONUNBUFFERED=1

WORKDIR /app
# Dependencies first, so a code change does not reinstall them.
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev

# Run as an ordinary user (uid 1000, the usual first user on a Linux host, so files in the
# mounted data folder belong to you). All state lives in /data.
RUN useradd --create-home --uid 1000 autobill \
    && mkdir /data && chown autobill:autobill /data
USER autobill
ENV AUTOBILL_DATA_DIR=/data \
    PATH="/opt/venv/bin:$PATH"
VOLUME ["/data"]

ENTRYPOINT ["autobill"]
CMD ["serve"]
