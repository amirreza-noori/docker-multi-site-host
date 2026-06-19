FROM haproxy:2.8
USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /var/lib/haproxy && chown haproxy:haproxy /var/lib/haproxy
COPY haproxy/assemble-config.sh /usr/local/bin/assemble-config.sh
RUN sed -i 's/\r$//' /usr/local/bin/assemble-config.sh && chmod +x /usr/local/bin/assemble-config.sh
ENTRYPOINT ["/usr/local/bin/assemble-config.sh"]
CMD []
