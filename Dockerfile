FROM python:3.13-slim

LABEL org.opencontainers.image.title="QuickLinks" \
      org.opencontainers.image.authors="Jordan Farmer" \
      org.opencontainers.image.source="https://github.com/falco1717/quicklinks"

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . ./
RUN mkdir -p /app/data

ENV DATA_DIR=/app/data \
    HOST=0.0.0.0 \
    PORT=6969
EXPOSE 6969
VOLUME ["/app/data"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:6969/api/catalog', timeout=3)" || exit 1
CMD ["python", "server.py"]
