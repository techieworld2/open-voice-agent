FROM python:3.12-slim

ARG VOICE_ENGINE_EXTRAS=

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    espeak-ng libsndfile1 ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY static ./static
COPY benchmarks ./benchmarks

RUN pip install --upgrade pip && if [ -n "$VOICE_ENGINE_EXTRAS" ]; then pip install ".[${VOICE_ENGINE_EXTRAS}]"; else pip install .; fi
EXPOSE 8000
CMD ["uvicorn","voice_engine.server.app:app","--host","0.0.0.0","--port","8000"]
