# syntax=docker/dockerfile:1
FROM python:3.11-slim AS build

WORKDIR /build
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY spec ./spec
RUN python -m pip install --no-cache-dir 'setuptools>=69' wheel build \
    && python -m build --wheel --no-isolation

FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
RUN groupadd --system matterhorn \
    && useradd --system --gid matterhorn --home-dir /app matterhorn
WORKDIR /app
COPY --from=build /build/dist/*.whl /tmp/
RUN python -m pip install --no-cache-dir \
      'fastapi>=0.115' 'httpx>=0.27' 'uvicorn>=0.30' 'psycopg[binary]>=3.2,<4' \
    && python -m pip install --no-cache-dir /tmp/matterhorn_memory-*.whl \
    && rm -f /tmp/*.whl \
    && chown matterhorn:matterhorn /app
USER matterhorn
EXPOSE 8000
CMD ["mh", "serve", "--host", "0.0.0.0", "--port", "8000", "--db", "matterhorn.db"]

FROM runtime AS console
CMD ["mh", "console", "--no-open", "--host", "0.0.0.0", "--port", "8000", "--db", "matterhorn.db"]

FROM runtime AS test
USER root
COPY . /app
RUN python -m pip install --no-cache-dir '.[dev,postgres]'
USER matterhorn
