FROM python:3.12-slim

# ffmpeg (with libass for captions and libflite for offline narration) + a caption font
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir ".[all]"
COPY config ./config

ENV PYTHONUNBUFFERED=1
VOLUME ["/app/data"]
CMD ["vw", "daemon"]
