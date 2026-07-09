FROM alpine:3.21

RUN apk add --no-cache \
    autossh \
    bash \
    haproxy \
    openssh-client \
    privoxy

COPY haproxy.cfg /etc/haproxy/haproxy.cfg
COPY haproxy-inbound.cfg /etc/haproxy/haproxy-inbound.cfg
COPY privoxy.config /etc/privoxy/config
COPY entrypoint.sh /entrypoint.sh

RUN sed -i 's/\r$//' /entrypoint.sh \
    && if [ "$(head -c 3 /entrypoint.sh | od -An -t x1 | tr -d ' \n')" = "efbbbf" ]; then tail -c +4 /entrypoint.sh > /tmp/entrypoint.sh && mv /tmp/entrypoint.sh /entrypoint.sh; fi \
    && chmod +x /entrypoint.sh

ENTRYPOINT ["/bin/bash", "/entrypoint.sh"]
