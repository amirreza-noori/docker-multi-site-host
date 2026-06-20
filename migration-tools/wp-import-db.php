<?php
/**
 * One-off WordPress database import from wp-export-db.php zip (CLI).
 */

declare(strict_types=1);

@ini_set('memory_limit', '2048M');
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

    function migration_resolve_import_zip(string $zip_name): string
    {
        if ($zip_name !== '' && $zip_name[0] === '/') {
            if (!preg_match('/^db_migrate_[a-f0-9]{32}\\.sql\\.zip$/', basename($zip_name))) {
                throw new RuntimeException('Invalid zip filename.');
            }
            if (!is_readable($zip_name)) {
                throw new RuntimeException('Zip file not found.');
            }

            return $zip_name;
        }

        return migration_resolve_zip_path(__DIR__, $zip_name);
    }

    function migration_resolve_zip_path(string $base_dir, string $zip_name): string
    {
        $zip_name = basename($zip_name);
        if (!preg_match('/^db_migrate_[a-f0-9]{32}\\.sql\\.zip$/', $zip_name)) {
            throw new RuntimeException('Invalid zip filename.');
        }

        $zip_path = $base_dir . DIRECTORY_SEPARATOR . $zip_name;
        if (!is_readable($zip_path)) {
            throw new RuntimeException('Zip file not found.');
        }

        return $zip_path;
    }

    function migration_writable_base_dir(string $zip_path): string
    {
        $zip_dir = dirname($zip_path);
        if (is_dir($zip_dir) && is_writable($zip_dir)) {
            return $zip_dir;
        }

        $wp_root = dirname(migration_find_wp_config_path());
        $uploads = $wp_root . '/wp-content/uploads';
        if (is_dir($uploads) && is_writable($uploads)) {
            return $uploads;
        }

        $tmpdir = rtrim(sys_get_temp_dir(), '/');
        if (is_dir($tmpdir) && is_writable($tmpdir)) {
            return $tmpdir;
        }

        throw new RuntimeException(
            'No writable directory for unzip. Fix ownership: chown application:application /app/wp-import-db.php /app/db_migrate_*.sql.zip — or use CLI: docker exec CONTAINER php /app/wp-import-db.php …'
        );
    }

    function migration_make_work_dir(string $base_dir): string
    {
        $dir = rtrim($base_dir, '/') . '/.wp-migrate-' . bin2hex(random_bytes(8));
        if (!mkdir($dir, 0700, true) && !is_dir($dir)) {
            throw new RuntimeException('Could not create work directory.');
        }

        return $dir;
    }

    function migration_unzip_sql(string $zip_path): string
    {
        if (!class_exists(ZipArchive::class)) {
            throw new RuntimeException('ZipArchive extension is not available.');
        }

        $zip = new ZipArchive();
        if ($zip->open($zip_path) !== true) {
            throw new RuntimeException('Could not open zip archive.');
        }

        if ($zip->numFiles !== 1) {
            $zip->close();
            throw new RuntimeException('Zip archive must contain exactly one SQL file.');
        }

        $entry_name = $zip->getNameIndex(0);
        if ($entry_name === false || substr(basename($entry_name), -4) !== '.sql') {
            $zip->close();
            throw new RuntimeException('Zip archive does not contain a .sql file.');
        }

        $extract_dir = migration_make_work_dir(migration_writable_base_dir($zip_path));
        $sql_name = basename($entry_name);
        $sql_path = $extract_dir . DIRECTORY_SEPARATOR . $sql_name;

        $stream = $zip->getStream($entry_name);
        if ($stream === false) {
            $zip->close();
            migration_remove_dir($extract_dir);
            throw new RuntimeException('Could not read SQL file from zip archive.');
        }

        $out = fopen($sql_path, 'wb');
        if ($out === false) {
            fclose($stream);
            $zip->close();
            migration_remove_dir($extract_dir);
            throw new RuntimeException('Could not write extracted SQL file.');
        }

        stream_copy_to_stream($stream, $out);
        fclose($stream);
        fclose($out);
        $zip->close();

        if (!is_readable($sql_path)) {
            migration_remove_dir($extract_dir);
            throw new RuntimeException('Extracted SQL file is not readable.');
        }

        return $sql_path;
    }

    function migration_remove_dir(string $dir): void
    {
        if (!is_dir($dir)) {
            return;
        }

        foreach (scandir($dir) ?: [] as $item) {
            if ($item === '.' || $item === '..') {
                continue;
            }
            $path = $dir . DIRECTORY_SEPARATOR . $item;
            if (is_dir($path)) {
                migration_remove_dir($path);
            } else {
                @unlink($path);
            }
        }

        @rmdir($dir);
    }

    function migration_find_mariadb_client(): ?string
    {
        if (!function_exists('exec')) {
            return null;
        }
        foreach (['mariadb', 'mysql'] as $cmd) {
            $paths = [];
            exec('command -v ' . escapeshellarg($cmd) . ' 2>/dev/null', $paths, $code);
            if ($code === 0 && isset($paths[0]) && $paths[0] !== '') {
                return $paths[0];
            }
        }

        return null;
    }

    function migration_import_via_mariadb_client(array $config, string $sql_path): bool
    {
        $binary = migration_find_mariadb_client();
        if ($binary === null) {
            return false;
        }

        $parsed = migration_parse_db_host($config['DB_HOST']);
        $command = [
            $binary,
            '-h', $parsed['socket'] !== null ? 'localhost' : $parsed['host'],
            '-u', $config['DB_USER'],
            '--password=' . $config['DB_PASSWORD'],
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
            0 => ['file', $sql_path, 'r'],
            1 => ['pipe', 'w'],
            2 => ['pipe', 'w'],
        ];

        $process = proc_open($command, $descriptors, $pipes);
        if (!is_resource($process)) {
            return false;
        }

        fclose($pipes[0]);
        $stdout = stream_get_contents($pipes[1]);
        fclose($pipes[1]);
        $stderr = stream_get_contents($pipes[2]);
        fclose($pipes[2]);
        $exit_code = proc_close($process);

        if ($exit_code !== 0) {
            $message = trim($stderr !== '' ? $stderr : $stdout);
            throw new RuntimeException($message !== '' ? $message : 'mariadb client import failed.');
        }

        return true;
    }

    function migration_import_database_stream(array $config, string $sql_path): void
    {
        $mysqli = migration_connect($config);
        $handle = fopen($sql_path, 'rb');
        if ($handle === false) {
            $mysqli->close();
            throw new RuntimeException('Could not open SQL file.');
        }

        $statement = '';
        try {
            while (($line = fgets($handle)) !== false) {
                $trimmed = trim($line);
                if ($trimmed === '' || strpos($trimmed, '--') === 0) {
                    continue;
                }

                $statement .= $line;
                if (substr(rtrim($line), -1) !== ';') {
                    continue;
                }

                if (!$mysqli->query($statement)) {
                    throw new RuntimeException('Import error: ' . $mysqli->error);
                }
                $statement = '';
            }
        } finally {
            fclose($handle);
            $mysqli->close();
        }
    }

    function migration_import_database(array $config, string $sql_path): void
    {
        if (migration_import_via_mariadb_client($config, $sql_path)) {
            return;
        }

        migration_import_database_stream($config, $sql_path);
    }
}

if (PHP_SAPI !== 'cli') {
    fwrite(STDERR, "CLI only. Example: docker exec CONTAINER php /app/wp-import-db.php dump.sql.zip\n");
    exit(1);
}

$zip_name = $argv[1] ?? '';
if ($zip_name === '' || $zip_name === '-h' || $zip_name === '--help') {
    fwrite(STDERR, "Usage: php wp-import-db.php <dump.sql.zip>\n");
    exit($zip_name === '' ? 1 : 0);
}

try {
    $zip_path = migration_resolve_import_zip($zip_name);
    $sql_file = migration_unzip_sql($zip_path);
    $temp_dir = dirname($sql_file);
    $config = migration_load_db_config(migration_find_wp_config_path());
    migration_import_database($config, $sql_file);
    migration_remove_dir($temp_dir);

    echo "Database import completed.\n";
    exit(0);
} catch (Throwable $e) {
    if (isset($temp_dir) && is_string($temp_dir)) {
        migration_remove_dir($temp_dir);
    }
    fwrite(STDERR, 'Import failed: ' . $e->getMessage() . PHP_EOL);
    exit(1);
}
