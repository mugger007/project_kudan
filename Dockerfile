FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /build
COPY requirements.txt /build/requirements.txt
RUN pip install --upgrade pip && pip wheel --wheel-dir /build/wheels -r /build/requirements.txt

FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    iproute2 \
    iptables \
    net-tools \
    openvpn \
    tini \
    gosu \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /build/wheels /wheels
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --find-links /wheels -r /app/requirements.txt && rm -rf /wheels

RUN useradd --create-home --home-dir /home/appuser --shell /bin/bash appuser

COPY . /app
RUN mkdir -p /data /app/logs && chown -R appuser:appuser /app /data

RUN chmod +x /app/entrypoint.sh

EXPOSE 8080

ENTRYPOINT ["/usr/bin/tini", "--", "/app/entrypoint.sh"]
