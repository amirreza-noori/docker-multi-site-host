# syntax=docker/dockerfile:1
FROM alpine:3.21

ENV TZ=Asia/Tehran

RUN apk add --no-cache zip bash curl mariadb-client tzdata \
    && cp /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo "$TZ" > /etc/timezone \
    && echo "*/10 * * * * /bin/bash /usr/local/bin/backup.sh" >> /etc/crontabs/root

COPY backup.sh /usr/local/bin/backup.sh
RUN chmod +x /usr/local/bin/backup.sh \
    && sed -i 's/\r$//' /usr/local/bin/backup.sh

CMD ["crond", "-f"]
