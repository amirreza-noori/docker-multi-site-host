FROM alpine:3.20
COPY scripts/bootstrap-core.sh /bootstrap-core.sh
# Normalize Windows line endings from the build context (repo may be edited on Windows)
RUN sed -i 's/\r$//' /bootstrap-core.sh
ENTRYPOINT ["/bin/sh", "/bootstrap-core.sh"]
