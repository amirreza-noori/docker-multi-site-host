# syntax=docker/dockerfile:1
FROM python:3.12-slim-bookworm

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends certbot curl; \
    rm -rf /var/lib/apt/lists/*; \
    mkdir -p /etc/letsencrypt/haproxy /etc/letsencrypt/webroot

COPY manage_certs.py /usr/local/bin/manage_certs.py
RUN chmod +x /usr/local/bin/manage_certs.py

ENV SITES_DIR=/sites \
    CERT_DIR=/etc/letsencrypt \
    CHECK_INTERVAL=43200

CMD ["/bin/sh", "-c", "python3 -m http.server 80 --directory /etc/letsencrypt/webroot & python3 /usr/local/bin/manage_certs.py"]
