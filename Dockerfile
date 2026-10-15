# Multi-arch (linux/amd64, linux/arm64). Kolibri is a pure-Python wheel, so no compile step.
FROM python:3.9-slim-bookworm
ARG KOLIBRI_VERSION=0.15.5
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    KOLIBRI_HOME=/tmp/kolibri-builder-home KOLIBRI_VERSION=${KOLIBRI_VERSION} \
    PYTHONPATH=/app FILES_ROOT=/mnt/idea-files EXPORT_DIR=/data/export WORK_DIR=/data/work PORT=8090
# ffprobe only is needed (codec check); Debian ships it in the ffmpeg package.
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir "kolibri==${KOLIBRI_VERSION}" "setuptools<70"
COPY builder/channel_builder /app/channel_builder
RUN groupadd -r -g 1000 builder && useradd -r -u 1000 -g 1000 builder \
    && mkdir -p /data/export /data/work && chown -R builder:builder /data
# Starts as root only to create/chown the (possibly root-owned) bind dirs, then
# drops to uid 1000 with setpriv (util-linux, already in the base image).
COPY builder/entrypoint.sh /usr/local/bin/entrypoint.sh
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
EXPOSE 8090
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request,os;urllib.request.urlopen('http://127.0.0.1:%s/healthz'%os.environ['PORT'],timeout=2)"
CMD ["python", "-m", "channel_builder.server"]
