# syntax=docker/dockerfile:1
FROM webdevops/php-apache:8.4

ENV PROVISION_CONTEXT=production

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends unzip; \
    rm -rf /var/lib/apt/lists/*; \
    pecl install timezonedb

ARG WORDPRESS_VERSION=6.8
RUN set -eux; \
    curl -fsSL "https://wordpress.org/wordpress-${WORDPRESS_VERSION}.tar.gz" -o /tmp/wordpress.tar.gz; \
    mkdir -p /app; \
    tar -xzf /tmp/wordpress.tar.gz -C /app --strip-components=1; \
    rm /tmp/wordpress.tar.gz

ARG WORDPRESS_LOCALE=en_US
ARG WORDPRESS_VERSION=6.8
RUN set -eux; \
    if [ "${WORDPRESS_LOCALE}" != "en_US" ]; then \
      mkdir -p /app/wp-content/languages; \
      curl -fsSL "https://downloads.wordpress.org/translation/core/${WORDPRESS_VERSION}/${WORDPRESS_LOCALE}.zip" -o /tmp/locale.zip; \
      unzip -oq /tmp/locale.zip -d /app/wp-content/languages; \
      rm /tmp/locale.zip; \
    fi

RUN chown -R application:application /app

COPY docker/wordpress-entrypoint.sh /usr/local/bin/wordpress-entrypoint.sh
RUN chmod +x /usr/local/bin/wordpress-entrypoint.sh \
    && sed -i 's/\r$//' /usr/local/bin/wordpress-entrypoint.sh

WORKDIR /app
ENTRYPOINT ["/bin/bash", "/usr/local/bin/wordpress-entrypoint.sh"]
