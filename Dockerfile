# Himal KB Mock Creator — web GUI + API container
# Build context: this folder (web-questions-creator/)
#  - pipeline/ = synced copies of mock_next.py, mock_audio_builder.py, magnific_mcp.py
#  - scripts/  = synced copies of tts.py, audio_convert.py
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pipeline/ /app/pipeline/
COPY scripts/ /app/scripts/
COPY web_creator.py /app/web_creator.py
COPY web_dashboard.html /app/web_dashboard.html

# TTS helper scripts must live where mock_audio_builder.py looks for them
RUN mkdir -p /root/.config/opencode/scripts \
    && cp /app/scripts/tts.py /app/scripts/audio_convert.py /root/.config/opencode/scripts/

RUN pip install --no-cache-dir fastapi "uvicorn[standard]" httpx pillow

ENV PORT=33445 \
    MOCK_ROOT=/app/data \
    PYTHONUNBUFFERED=1

EXPOSE 33445

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:33445/api/health', timeout=5)" || exit 1

CMD ["python", "/app/web_creator.py"]
