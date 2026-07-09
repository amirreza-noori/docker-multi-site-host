#!/bin/bash
set -euo pipefail

SITES_CONF="${SITES_CONF:-/etc/backup/sites.conf}"
SITES_ROOT="${SITES_ROOT:-/sites}"
BACKUP_DIR="${BACKUP_DIR:-/backup}"
MARIADB_HOST="${MARIADB_HOST:-mariadb}"
MARIADB_ROOT_PASSWORD="${MARIADB_ROOT_PASSWORD:?MARIADB_ROOT_PASSWORD is required}"
BACKUP_FTP_SERVER="${BACKUP_FTP_SERVER:-}"
BACKUP_FTP_USER="${BACKUP_FTP_USER:-}"
BACKUP_FTP_PASS="${BACKUP_FTP_PASS:-}"
BACKUP_FTP_PROXY="${BACKUP_FTP_PROXY:-}"
BACKUP_FULL_HOUR="${BACKUP_FULL_HOUR:-4}"

mkdir -p "$BACKUP_DIR"

is_disabled() {
    local val
    val=$(echo "$1" | tr '[:upper:]' '[:lower:]')
    case "$val" in
        0 | off | disabled | disable | - | none) return 0 ;;
        "") return 0 ;;
        *) return 1 ;;
    esac
}

ftp_remote_path() {
    local file=$1
    echo "${file#"$BACKUP_DIR"/}"
}

ftp_curl() {
    local -a opts=( -s --ftp-skip-pasv-ip )
    if [ -n "$BACKUP_FTP_PROXY" ]; then
        opts+=( --proxy "$BACKUP_FTP_PROXY" )
    fi
    curl "${opts[@]}" "$@"
}

ftp_upload() {
    local file=$1
    local remote_path
    remote_path=$(ftp_remote_path "$file")
    ftp_curl --ftp-create-dirs -T "$file" --user "$BACKUP_FTP_USER:$BACKUP_FTP_PASS" \
        "ftp://$BACKUP_FTP_SERVER/$remote_path" -o /dev/null -w "UPLOAD $remote_path %{http_code}\n"
}

ftp_delete() {
    local file=$1
    local remote_path remote_dir remote_name
    remote_path=$(ftp_remote_path "$file")
    remote_dir=$(dirname "$remote_path")
    remote_name=$(basename "$remote_path")
    ftp_curl -u "$BACKUP_FTP_USER:$BACKUP_FTP_PASS" "ftp://$BACKUP_FTP_SERVER/$remote_dir/" \
        -Q "-DELE $remote_name" -o /dev/null -w "DELETE $remote_path %{http_code}\n"
}

last_backup_epoch() {
    local state_file=$1
    if [ -f "$state_file" ]; then
        cat "$state_file"
    else
        echo 0
    fi
}

mark_backup_done() {
    local state_file=$1
    mkdir -p "$(dirname "$state_file")"
    date +%s >"$state_file"
}

should_run_interval() {
    local state_file=$1
    local interval=$2
    local unit=$3
    local now last elapsed

    now=$(date +%s)
    last=$(last_backup_epoch "$state_file")

    if [ "$last" -eq 0 ]; then
        return 0
    fi

    elapsed=$((now - last))
    if [ "$unit" = "minutes" ]; then
        [ "$elapsed" -ge $((interval * 60)) ]
        return
    fi

    [ "$elapsed" -ge $((interval * 86400)) ]
}

should_keep_sql_file() {
    local file=$1
    local filename
    filename=$(basename "$file")
    local datetime
    datetime=$(echo "$filename" | sed -e 's/db_backup_//' -e 's/.sql.zip//')
    local formatted_datetime
    formatted_datetime=$(echo "$datetime" | sed 's/\(....\)\(..\)\(..\)_\(..\)\(..\)/\1-\2-\3 \4:\5/')
    local file_time
    file_time=$(date -d "$formatted_datetime" +%s)
    local current_time
    current_time=$(date +%s)
    local age=$(((current_time - file_time) / 60))

    if [ $age -lt 60 ]; then
        return 0
    elif [ $age -lt 480 ] && [[ "$datetime" =~ [0-9]{8}_[0-9]{2}00 ]]; then
        return 0
    elif [ $age -lt 10080 ] && [[ "$datetime" =~ [0-9]{8}_00 ]]; then
        return 0
    elif [ $age -lt 43200 ] && [[ "$datetime" =~ [0-9]{6}01_00 ]]; then
        return 0
    elif [ $age -lt 43200 ] && [[ "$datetime" =~ [0-9]{6}08_00 ]]; then
        return 0
    elif [ $age -lt 43200 ] && [[ "$datetime" =~ [0-9]{6}15_00 ]]; then
        return 0
    elif [ $age -lt 43200 ] && [[ "$datetime" =~ [0-9]{6}22_00 ]]; then
        return 0
    elif [ $age -lt 157680 ] && [[ "$datetime" =~ [0-9]{6}01_00 ]]; then
        return 0
    fi
    return 1
}

should_keep_zip_file() {
    local file=$1
    local filename
    filename=$(basename "$file")
    local datetime
    datetime=$(echo "$filename" | sed -e 's/files_backup_//' -e 's/.zip//')
    local formatted_datetime
    formatted_datetime=$(echo "$datetime" | sed 's/\(....\)\(..\)\(..\)_\(..\)\(..\)/\1-\2-\3 \4:\5/')
    local file_time
    file_time=$(date -d "$formatted_datetime" +%s)
    local current_time
    current_time=$(date +%s)
    local age=$(((current_time - file_time) / 86400))

    if [ $age -lt 7 ]; then
        return 0
    elif [ $age -lt 30 ] && [[ "$datetime" =~ [0-9]{6}(01|08|15|22)_ ]]; then
        return 0
    elif [ $age -lt 180 ] && [[ "$datetime" =~ [0-9]{6}01_ ]]; then
        return 0
    fi
    return 1
}

prune_backups() {
    local pattern=$1
    local keep_fn=$2
    local file

    for file in $pattern; do
        [ -e "$file" ] || continue
        if ! $keep_fn "$file"; then
            rm -f "$file"
            if [ -n "$BACKUP_FTP_SERVER" ]; then
                ftp_delete "$file"
            fi
        fi
    done
}

backup_site_database() {
    local folder=$1
    local database=$2
    local site_backup_dir="$BACKUP_DIR/$folder"
    local timestamp
    timestamp=$(date +"%Y%m%d_%H%M")
    local db_file="$site_backup_dir/db_backup_${timestamp}.sql"

    mkdir -p "$site_backup_dir"
    mariadb-dump -h "$MARIADB_HOST" -uroot -p"$MARIADB_ROOT_PASSWORD" "$database" >"$db_file"
    zip -j "${db_file}.zip" "$db_file"
    rm "$db_file"

    if [ -n "$BACKUP_FTP_SERVER" ]; then
        ftp_upload "${db_file}.zip"
    fi

    mark_backup_done "$site_backup_dir/.last_db_backup"
    prune_backups "$site_backup_dir/db_backup_*.sql.zip" should_keep_sql_file
}

backup_site_files() {
    local folder=$1
    local paths_csv=$2
    local ignore_patterns=$3
    local site_dir="$SITES_ROOT/$folder"
    local site_backup_dir="$BACKUP_DIR/$folder"
    local timestamp
    timestamp=$(date +"%Y%m%d_%H%M")
    local zip_file="$site_backup_dir/files_backup_${timestamp}.zip"
    local ignore_options=""
    local path

    mkdir -p "$site_backup_dir"

    if [ ! -d "$site_dir" ]; then
        echo "Skip files backup: site directory not found ($site_dir)"
        return
    fi

    cd "$site_dir"

    for path in $ignore_patterns; do
        ignore_options="$ignore_options -x \"$path\""
    done

    local zip_targets=()
    IFS=',' read -ra paths <<< "$paths_csv"
    for path in "${paths[@]}"; do
        path="${path// /}"
        [ -n "$path" ] || continue
        if [ -e "$path" ]; then
            zip_targets+=("$path")
        else
            echo "Skip missing path for $folder: $path"
        fi
    done

    if [ ${#zip_targets[@]} -eq 0 ]; then
        echo "Skip files backup for $folder: no paths to archive"
        return
    fi

    eval "zip -qr0 \"$zip_file\" ${zip_targets[*]} $ignore_options"

    if [ -n "$BACKUP_FTP_SERVER" ]; then
        ftp_upload "$zip_file"
    fi

    mark_backup_done "$site_backup_dir/.last_files_backup"
    prune_backups "$site_backup_dir/files_backup_*.zip" should_keep_zip_file
}

if [ ! -f "$SITES_CONF" ]; then
    echo "No backup config at $SITES_CONF"
    exit 0
fi

CURRENT_HOUR=$(date +%H)
CURRENT_MINUTE=$(date +%M)
FILES_CHECK_WINDOW=0
if [ "$CURRENT_HOUR" -eq "$BACKUP_FULL_HOUR" ] && [ "$CURRENT_MINUTE" -lt 10 ]; then
    FILES_CHECK_WINDOW=1
fi

while IFS= read -r line || [ -n "$line" ]; do
    line="${line%%#*}"
    line="$(echo "$line" | xargs)"
    [ -z "$line" ] && continue

    IFS='|' read -r folder database paths_csv ignore_patterns db_interval files_interval <<< "$line"
    folder="${folder// /}"
    database="${database// /}"
    paths_csv="${paths_csv// /}"
    db_interval="${db_interval:-0}"
    files_interval="${files_interval:-0}"

    if [ -z "$folder" ]; then
        echo "Invalid backup config line (missing folder): $line"
        continue
    fi

    site_backup_dir="$BACKUP_DIR/$folder"

    if ! is_disabled "$db_interval"; then
        if [ -z "$database" ] || [ "$database" = "-" ]; then
            echo "Skip database backup for $folder: database name is required"
        elif should_run_interval "$site_backup_dir/.last_db_backup" "$db_interval" minutes; then
            echo "Database backup: $folder ($database), every ${db_interval} minutes"
            backup_site_database "$folder" "$database"
        else
            echo "Skip database backup for $folder: interval ${db_interval} minutes not reached"
        fi
    else
        echo "Database backup disabled for $folder"
    fi

    if is_disabled "$files_interval"; then
        echo "Files backup disabled for $folder"
        continue
    fi

    if [ "$FILES_CHECK_WINDOW" -eq 0 ]; then
        continue
    fi

    if [ -z "$paths_csv" ] || [ "$paths_csv" = "-" ]; then
        echo "Skip files backup for $folder: paths are required when files backup is enabled"
        continue
    fi

    if should_run_interval "$site_backup_dir/.last_files_backup" "$files_interval" days; then
        echo "Files backup: $folder ($paths_csv), every ${files_interval} days"
        backup_site_files "$folder" "$paths_csv" "$ignore_patterns"
    else
        echo "Skip files backup for $folder: interval ${files_interval} days not reached"
    fi
done < "$SITES_CONF"

sync
