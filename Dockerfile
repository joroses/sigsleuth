# Node builds the web page; the app itself needs only Python and the built page.
# The page is the same for every platform, so build it natively.
FROM --platform=$BUILDPLATFORM node:22-slim AS page
ENV COREPACK_ENABLE_DOWNLOAD_PROMPT=0
WORKDIR /frontend
RUN corepack enable
COPY src/frontend/package.json src/frontend/pnpm-lock.yaml src/frontend/.npmrc ./
RUN pnpm install --frozen-lockfile
COPY src/frontend ./
RUN pnpm run build

FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never PYTHONUNBUFFERED=1 \
    SIGSLEUTH_DATABASE_PATH=/data/sigsleuth.db SIGSLEUTH_API_HOST=0.0.0.0 SIGSLEUTH_API_PORT=8000
WORKDIR /app
# Dependencies first, so a code change reuses their layer.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-dev --no-install-project
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src/backend src/backend
# The app serves the page from src/frontend/dist beside its code.
COPY --from=page /frontend/dist src/frontend/dist
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev
# A new named volume takes /data's owner, so the app can write its database.
RUN useradd --uid 1000 --create-home app && mkdir /data && chown app /data
USER app
EXPOSE 8000
CMD ["/app/.venv/bin/python", "-m", "sigsleuth", "run"]
