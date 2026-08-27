FROM python:3.13-slim

LABEL org.opencontainers.image.title="QuickLinks" \
      org.opencontainers.image.authors="Jordan Farmer" \
      org.opencontainers.image.source="https://github.com/falco1717/quicklinks"

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . ./

# QuickLinks needs no privileges: it binds an unprivileged port and writes only
# to DATA_DIR. The application directory stays owned by root and read-only to
# the service, so a flaw in the app cannot rewrite the code serving it.
#
# The UID is fixed at 1000 rather than left to useradd so that a bind-mounted
# data directory keeps one owner across rebuilds. An existing deployment whose
# ./data was created by the old root container needs one command once:
#   sudo chown -R 1000:1000 /path/to/data
# Without it the container stops at startup with a message naming the directory,
# the uid, and the reason -- see ensure_data_directory().
RUN useradd --system --uid 1000 --user-group --no-create-home quicklinks \
 && mkdir -p /app/data \
 && chown quicklinks:quicklinks /app/data
USER 1000:1000

ENV DATA_DIR=/app/data \
    HOST=0.0.0.0 \
    PORT=6969
EXPOSE 6969
VOLUME ["/app/data"]
# Exec form: no shell to word-split, and a non-zero exit from python already
# marks the check failed, so no `|| exit 1` is needed.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:6969/api/catalog', timeout=3)"]
CMD ["python", "server.py"]
