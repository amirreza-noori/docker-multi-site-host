FROM alpine:3.21

RUN apk add --no-cache \
    autossh \
    bash \
    haproxy \
    openssh-client \
    privoxy

COPY haproxy.cfg /etc/haproxy/haproxy.cfg
COPY privoxy.config /etc/privoxy/config
COPY entrypoint.sh /entrypoint.sh

RUN sed -i 's/\r$//' /entrypoint.sh \
    && sed -i '1s/^\xEF\xBB\xBF//' /entrypoint.sh \
    && chmod +x /entrypoint.sh

ENTRYPOINT ["/bin/bash", "/entrypoint.sh"]
