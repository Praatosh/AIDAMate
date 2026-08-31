# AIDA-MATE — portable container image.
#
# Self-contained: pulls only from PyPI, bakes in no secrets, and reads all
# configuration from the environment at runtime. Built and run identically
# on any machine with Docker — nothing here depends on this host.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# hatchling (the build backend) requires the file declared as `readme` in
# pyproject.toml to exist at build time, so it comes along with the app code.
COPY pyproject.toml README.md ./
COPY app ./app

# openai-agents is the only model-provider extra installed by default,
# matching MODEL_PROVIDER=openai's default in app/core/config.py. Add
# `.[openai,anthropic]` here if MODEL_PROVIDER=anthropic is ever used.
RUN pip install ".[openai]"

# /data is where REVIEW_STORE_PATH and LINEAR_TOKEN_STORE_PATH should point —
# see docker-compose.yml — so a named volume can survive container recreation
# (a rebuild, a redeploy, moving to a different host) instead of losing all
# review history and Linear OAuth installations every time.
RUN mkdir -p /data \
    && useradd --create-home --shell /bin/bash appuser \
    && chown -R appuser:appuser /app /data
USER appuser

VOLUME ["/data"]
EXPOSE 8000

# Uses the stdlib rather than curl/wget — neither is installed in the slim
# base image, and adding one just for this would be a needless extra layer.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=3)" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
