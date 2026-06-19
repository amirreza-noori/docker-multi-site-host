-- Run after creating a site (edit names/passwords first):
-- docker exec -i mariadb mariadb -uroot -p"$MARIADB_ROOT_PASSWORD" < "$SITES_DIR/<folder>/database.sql"
--
-- Each site gets its own database and user with access only to that database.

CREATE DATABASE IF NOT EXISTS `example_com_wp` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE USER IF NOT EXISTS 'wp_example_com'@'%' IDENTIFIED BY 'change-me-db' WITH MAX_USER_CONNECTIONS 4;
GRANT ALL PRIVILEGES ON `example_com_wp`.* TO 'wp_example_com'@'%';

FLUSH PRIVILEGES;
