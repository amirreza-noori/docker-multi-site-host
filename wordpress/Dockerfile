# syntax=docker/dockerfile:1
ARG PHP_VERSION=8.4
FROM webdevops/php-apache:${PHP_VERSION}

ARG PHP_VERSION
ENV PROVISION_CONTEXT=production

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    set -eux; \
    if [ "${PHP_VERSION}" = "7.4" ]; then \
      sed -i 's|deb.debian.org/debian|archive.debian.org/debian|g' /etc/apt/sources.list; \
      sed -i 's|security.debian.org/debian-security|archive.debian.org/debian-security|g' /etc/apt/sources.list; \
      sed -i '/buster-updates/d' /etc/apt/sources.list; \
      printf '%s\n' 'Acquire::Check-Valid-Until "false";' > /etc/apt/apt.conf.d/99no-check-valid-until; \
    fi; \
    apt-get update; \
    apt-get install -y --no-install-recommends unzip; \
    rm -rf /var/lib/apt/lists/*; \
    pecl install timezonedb; \
    pecl install igbinary

# Pinned release — rebuild image to upgrade core (sites block wordpress.org)
ARG WORDPRESS_VERSION=7.1.2
RUN set -eux; \
    curl -fsSL "https://wordpress.org/wordpress-${WORDPRESS_VERSION}.tar.gz" -o /tmp/wordpress.tar.gz; \
    mkdir -p /app; \
    tar -xzf /tmp/wordpress.tar.gz -C /app --strip-components=1; \
    rm /tmp/wordpress.tar.gz

ARG WORDPRESS_LOCALE=en_US
ARG WORDPRESS_VERSION=7.1.2
RUN set -eux; \
    mkdir -p /opt/wordpress-languages; \
    if [ "${WORDPRESS_LOCALE}" != "en_US" ]; then \
      mkdir -p /app/wp-content/languages; \
      curl -fsSL "https://downloads.wordpress.org/translation/core/${WORDPRESS_VERSION}/${WORDPRESS_LOCALE}.zip" -o /tmp/locale.zip; \
      unzip -oq /tmp/locale.zip -d /app/wp-content/languages; \
      rm /tmp/locale.zip; \
      cp -a /app/wp-content/languages/. /opt/wordpress-languages/; \
    fi

# Core readable by PHP (application) but not writable — only wp-content stays writable.
# Directory write bit off prevents create/delete/replace of core files.
RUN set -eux; \
    chown -R application:application /app /opt/wordpress-languages; \
    find /app -type d -exec chmod 555 {} +; \
    find /app -type f -exec chmod 444 {} +; \
    find /app/wp-content -type d -exec chmod 755 {} +; \
    find /app/wp-content -type f -exec chmod 644 {} +; \
    chmod 755 /app

COPY docker/wordpress-entrypoint.sh /usr/local/bin/wordpress-entrypoint.sh
RUN chmod +x /usr/local/bin/wordpress-entrypoint.sh \
    && sed -i 's/\r$//' /usr/local/bin/wordpress-entrypoint.sh

WORKDIR /app
ENTRYPOINT ["/bin/bash", "/usr/local/bin/wordpress-entrypoint.sh"]
