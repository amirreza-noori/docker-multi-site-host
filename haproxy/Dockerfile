# syntax=docker/dockerfile:1
FROM haproxy:2.8
USER root
RUN mkdir -p /var/lib/haproxy && chown haproxy:haproxy /var/lib/haproxy
COPY haproxy/assemble-config.sh /usr/local/bin/assemble-config.sh
RUN sed -i 's/\r$//' /usr/local/bin/assemble-config.sh && chmod +x /usr/local/bin/assemble-config.sh
ENTRYPOINT ["/usr/local/bin/assemble-config.sh"]
CMD []
