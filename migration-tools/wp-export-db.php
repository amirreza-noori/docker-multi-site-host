<?php
/**
 * One-off WordPress database export (CLI).
 */

declare(strict_types=1);

const MIGRATION_MEMORY_LIMIT = '2048M';

@ini_set('memory_limit', MIGRATION_MEMORY_LIMIT);
@set_time_limit(0);

if (!function_exists('migration_load_db_config')) {
    function migration_load_db_config(string $wp_config_path): array
    {
        if (!is_readable($wp_config_path)) {
            throw new RuntimeException('wp-config.php not found or not readable.');
        }

        $content = file_get_contents($wp_config_path);
        if ($content === false) {
            throw new RuntimeException('Could not read wp-config.php.');
        }

        $keys = ['DB_NAME', 'DB_USER', 'DB_PASSWORD', 'DB_HOST', 'DB_CHARSET'];
        $config = [];

        foreach ($keys as $key) {
            $pattern = "/define\\s*\\(\\s*['\"]{$key}['\"]\\s*,\\s*['\"](.*?)['\"]\\s*\\)/s";
            if (!preg_match($pattern, $content, $matches)) {
                throw new RuntimeException("Missing {$key} in wp-config.php.");
            }
            $config[$key] = stripcslashes($matches[1]);
        }

        if ($config['DB_NAME'] === '' || $config['DB_USER'] === '') {
            throw new RuntimeException('Database name or user is empty in wp-config.php.');
        }

        return $config;
    }

    function migration_parse_db_host(string $db_host): array
    {
        $host = $db_host;
        $port = 3306;
        $socket = null;

        if ($host !== '' && $host[0] === '/') {
            return ['host' => 'localhost', 'port' => $port, 'socket' => $host];
        }

        if (preg_match('/^(.*?):(\\d+)$/', $host, $matches)) {
            $host = $matches[1];
            $port = (int) $matches[2];
        }

        return ['host' => $host, 'port' => $port, 'socket' => $socket];
    }

    function migration_connect(array $config): mysqli
    {
        $parsed = migration_parse_db_host($config['DB_HOST']);
        mysqli_report(MYSQLI_REPORT_ERROR | MYSQLI_REPORT_STRICT);

        if ($parsed['socket'] !== null) {
            $mysqli = new mysqli('localhost', $config['DB_USER'], $config['DB_PASSWORD'], $config['DB_NAME'], $port = 0, $parsed['socket']);
        } else {
            $mysqli = new mysqli($parsed['host'], $config['DB_USER'], $config['DB_PASSWORD'], $config['DB_NAME'], $parsed['port']);
        }

        $charset = $config['DB_CHARSET'] !== '' ? $config['DB_CHARSET'] : 'utf8mb4';
        $mysqli->set_charset($charset);

        return $mysqli;
    }

    function migration_find_wp_config_path(): string
    {
        foreach ([__DIR__, '/app'] as $dir) {
            $path = rtrim($dir, '/') . '/wp-config.php';
            if (is_readable($path)) {
                return $path;
            }
        }

        throw new RuntimeException('wp-config.php not found (checked script directory and /app).');
    }

    function migration_random_basename(string $prefix, string $extension): string
    {
        return $prefix . bin2hex(random_bytes(16)) . $extension;
    }

    function migration_find_dump_binary(): ?string
    {
        foreach (['mariadb-dump', 'mysqldump'] as $cmd) {
            if (!function_exists('exec')) {
                break;
            }
            $paths = [];
            exec('command -v ' . escapeshellarg($cmd) . ' 2>/dev/null', $paths, $code);
            if ($code === 0 && isset($paths[0]) && $paths[0] !== '') {
                return $paths[0];
            }
        }

        return null;
    }

    function migration_export_database_via_dump_binary(array $config, string $dump_path): bool
    {
        $binary = migration_find_dump_binary();
        if ($binary === null) {
            return false;
        }

        $parsed = migration_parse_db_host($config['DB_HOST']);
        $command = [
            $binary,
            '-h', $parsed['socket'] !== null ? 'localhost' : $parsed['host'],
            '-u', $config['DB_USER'],
            '--password=' . $config['DB_PASSWORD'],
            '--single-transaction',
            '--quick',
            '--skip-lock-tables',
            '--default-character-set=' . ($config['DB_CHARSET'] !== '' ? $config['DB_CHARSET'] : 'utf8mb4'),
            $config['DB_NAME'],
        ];

        if ($parsed['socket'] !== null) {
            $command[] = '--socket=' . $parsed['socket'];
        } else {
            $command[] = '-P';
            $command[] = (string) $parsed['port'];
        }

        $descriptors = [
            0 => ['pipe', 'r'],
            1 => ['file', $dump_path, 'w'],
            2 => ['pipe', 'w'],
        ];

        $process = proc_open($command, $descriptors, $pipes);
        if (!is_resource($process)) {
            return false;
        }

        fclose($pipes[0]);
        $stderr = stream_get_contents($pipes[2]);
        fclose($pipes[2]);
        $exit_code = proc_close($process);

        if ($exit_code !== 0 || !is_readable($dump_path) || filesize($dump_path) === 0) {
            @unlink($dump_path);
            if ($stderr !== '') {
                throw new RuntimeException(trim($stderr));
            }
            return false;
        }

        return true;
    }

    function migration_sql_value(mysqli $mysqli, $value): string
    {
        if ($value === null) {
            return 'NULL';
        }

        return "'" . $mysqli->real_escape_string((string) $value) . "'";
    }

    function migration_export_database_via_php(array $config, string $dump_path, mysqli $mysqli): void
    {
        $handle = fopen($dump_path, 'wb');
        if ($handle === false) {
            throw new RuntimeException('Could not create SQL dump file.');
        }

        try {
            fwrite($handle, "-- WordPress database export\n");
            fwrite($handle, '-- Host: ' . $config['DB_HOST'] . "\n");
            fwrite($handle, '-- Database: ' . $config['DB_NAME'] . "\n");
            fwrite($handle, '-- Generated: ' . gmdate('c') . "\n\n");
            fwrite($handle, "SET NAMES {$config['DB_CHARSET']};\n");
            fwrite($handle, "SET foreign_key_checks = 0;\n");
            fwrite($handle, "SET sql_mode = 'NO_AUTO_VALUE_ON_ZERO';\n\n");

            $tables = [];
            $result = $mysqli->query('SHOW TABLES');
            while ($row = $result->fetch_row()) {
                $tables[] = $row[0];
            }
            $result->free();

            foreach ($tables as $table) {
                $escaped_table = '`' . str_replace('`', '``', $table) . '`';
                $create = $mysqli->query("SHOW CREATE TABLE {$escaped_table}")->fetch_assoc();
                fwrite($handle, "DROP TABLE IF EXISTS {$escaped_table};\n");
                fwrite($handle, $create['Create Table'] . ";\n\n");

                $columns = [];
                $col_result = $mysqli->query("SHOW COLUMNS FROM {$escaped_table}");
                while ($col = $col_result->fetch_assoc()) {
                    $columns[] = '`' . str_replace('`', '``', $col['Field']) . '`';
                }
                $col_result->free();

                if ($columns === []) {
                    fwrite($handle, "\n");
                    continue;
                }

                $column_list = implode(', ', $columns);
                $rows = $mysqli->query("SELECT * FROM {$escaped_table}", MYSQLI_USE_RESULT);
                if ($rows === false) {
                    throw new RuntimeException("Could not read data from {$table}.");
                }

                while ($row = $rows->fetch_row()) {
                    $values = [];
                    foreach ($row as $value) {
                        $values[] = migration_sql_value($mysqli, $value);
                    }
                    fwrite(
                        $handle,
                        "INSERT INTO {$escaped_table} ({$column_list}) VALUES (" . implode(', ', $values) . ");\n"
                    );
                    unset($values, $row);
                }

                $rows->free();
                fwrite($handle, "\n");
            }

            fwrite($handle, "SET foreign_key_checks = 1;\n");
        } finally {
            fclose($handle);
        }
    }

    function migration_export_database(array $config): string
    {
        $dump_path = __DIR__ . '/' . migration_random_basename('db_migrate_', '.sql');

        if (migration_export_database_via_dump_binary($config, $dump_path)) {
            return $dump_path;
        }

        $mysqli = migration_connect($config);
        try {
            migration_export_database_via_php($config, $dump_path, $mysqli);
        } finally {
            $mysqli->close();
        }

        return $dump_path;
    }

    function migration_zip_file(string $source_path): string
    {
        if (!class_exists(ZipArchive::class)) {
            throw new RuntimeException('ZipArchive extension is not available.');
        }

        $zip_path = __DIR__ . '/' . migration_random_basename('db_migrate_', '.sql.zip');
        $zip = new ZipArchive();
        if ($zip->open($zip_path, ZipArchive::CREATE | ZipArchive::OVERWRITE) !== true) {
            throw new RuntimeException('Could not create zip archive.');
        }

        $zip->addFile($source_path, basename($source_path));
        $zip->close();

        return $zip_path;
    }
}

if (PHP_SAPI !== 'cli') {
    fwrite(STDERR, "CLI only. Example: docker exec CONTAINER php /app/wp-export-db.php\n");
    exit(1);
}

try {
    $config = migration_load_db_config(migration_find_wp_config_path());
    $sql_file = migration_export_database($config);
    $zip_path = migration_zip_file($sql_file);
    @unlink($sql_file);

    echo "Created: {$zip_path}\n";
    exit(0);
} catch (Throwable $e) {
    fwrite(STDERR, 'Export failed: ' . $e->getMessage() . PHP_EOL);
    exit(1);
}
