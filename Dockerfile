# Himal KB Mock Creator — SaaS container (PocketBase GUI/API + job worker)
# Build context: this folder (web-questions-creator/)
#   pb_hooks/   -> PocketBase JS hooks (schema bootstrap, /api/creator/*, GUI routes)
#   pb_public/  -> creator GUI served by PocketBase at /creator
#   pipeline/   -> mock_next.py, mock_audio_builder.py, magnific_mcp.py
#   scripts/    -> tts.py, audio_convert.py
#   worker.py   -> polls PocketBase for queued mock_jobs and runs the pipeline
FROM python:3.12-slim

ARG PB_VERSION=0.39.10
ARG PB_PLATFORM=linux_amd64

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg unzip \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

ADD https://github.com/pocketbase/pocketbase/releases/download/v${PB_VERSION}/pocketbase_${PB_VERSION}_${PB_PLATFORM}.zip /tmp/pb.zip
RUN unzip /tmp/pb.zip -d /opt/pb && rm /tmp/pb.zip && chmod +x /opt/pb/pocketbase

COPY pb_hooks/ /app/pb_hooks/
COPY pb_public/ /app/pb_public/
COPY pipeline/ /app/pipeline/
COPY scripts/ /app/scripts/
COPY worker.py /app/worker.py
COPY entrypoint.sh /app/entrypoint.sh

# TTS helper scripts must live where mock_audio_builder.py looks for them
RUN mkdir -p /root/.config/opencode/scripts \
    && cp /app/scripts/tts.py /app/scripts/audio_convert.py /root/.config/opencode/scripts/ \
    && pip install --no-cache-dir httpx pillow pymupdf

ENV PB_URL=http://127.0.0.1:8090 \
    PB_PORT=8090 \
    WORK_ROOT=/app/data \
    PYTHONUNBUFFERED=1

EXPOSE 8090

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8090/api/creator/ping', timeout=5)" || exit 1

CMD ["bash", "/app/entrypoint.sh"]
