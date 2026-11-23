# RKP API: the compiler and its HTTP API. Small: Python slim, git for fetching repositories, no build tools.
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY repo_intel ./repo_intel
RUN pip install --no-cache-dir ".[azure]" \
 && useradd --uid 10001 --create-home rkp \
 && mkdir -p /data && chown rkp /data

USER rkp
ENV PYTHONUNBUFFERED=1 \
    RKP_API_HOST=0.0.0.0 \
    RKP_API_PORT=8787 \
    RKP_DATA_DIR=/data
EXPOSE 8787
CMD ["python", "-m", "repo_intel.serve"]
