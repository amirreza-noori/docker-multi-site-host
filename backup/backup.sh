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

# Per-site intervals from sites.conf (used by retention keepers)
CURRENT_DB_INTERVAL=10
CURRENT_FILES_INTERVAL=7

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
    local -a opts=( -s --ftp-skip-pasv-ip --connect-timeout 30 )
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

# Delete on FTP using the same path layout as upload
ftp_delete() {
    local file=$1
    local remote_path remote_dir remote_name code
    remote_path=$(ftp_remote_path "$file")
    remote_dir=$(dirname "$remote_path")
    remote_name=$(basename "$remote_path")

    code=$(ftp_curl --user "$BACKUP_FTP_USER:$BACKUP_FTP_PASS" \
        "ftp://$BACKUP_FTP_SERVER/$remote_dir/" \
        -Q "-DELE $remote_name" -o /dev/null -w "%{http_code}" || true)
    echo "DELETE $remote_path ${code:-err}"
    # curl FTP "http_code" may be DELE (250) or the following transfer (226)
    case "$code" in
        250 | 226 | 200) return 0 ;;
        *) return 1 ;;
    esac
}

ftp_list_names() {
    local remote_dir=$1
    ftp_curl --user "$BACKUP_FTP_USER:$BACKUP_FTP_PASS" --list-only \
        "ftp://$BACKUP_FTP_SERVER/$remote_dir/"
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
        if [ "$elapsed" -ge $((interval * 60)) ]; then
            return 0
        fi
        return 1
    fi

    if [ "$elapsed" -ge $((interval * 86400)) ]; then
        return 0
    fi
    return 1
}

# YYYYMMDD_HHMM → epoch (BusyBox date -D, else GNU date -d)
backup_timestamp_epoch() {
    local datetime=$1
    local formatted epoch
    [[ "$datetime" =~ ^[0-9]{8}_[0-9]{4}$ ]] || return 1
    formatted="${datetime:0:4}-${datetime:4:2}-${datetime:6:2} ${datetime:9:2}:${datetime:11:2}:00"
    if epoch=$(date -D '%Y-%m-%d %H:%M:%S' -d "$formatted" +%s 2>/dev/null); then
        echo "$epoch"
        return 0
    fi
    if epoch=$(date -d "$formatted" +%s 2>/dev/null); then
        echo "$epoch"
        return 0
    fi
    return 1
}

# Minutes-of-day aligned to step (derived from db_interval)
is_aligned_minutes() {
    local hh=$1
    local mm=$2
    local step=$3
    local mod
    mod=$(( (10#$hh * 60 + 10#$mm) % step ))
    [ "$mod" -eq 0 ]
}

# SQL retention driven by CURRENT_DB_INTERVAL (minutes):
#   all for ~6 intervals (min 1h) → grid of ~12 intervals (min 2h) until 24h
#   → every 2 days until 30d → monthly until 6mo → Jan/Jul
should_keep_sql_file() {
    local file=$1
    local filename datetime file_time now age_min month day hh mm
    local interval keep_all_until step

    interval=${CURRENT_DB_INTERVAL:-10}
    if ! [[ "$interval" =~ ^[1-9][0-9]*$ ]]; then
        interval=10
    fi

    keep_all_until=$((interval * 6))
    if [ "$keep_all_until" -lt 60 ]; then
        keep_all_until=60
    fi
    if [ "$keep_all_until" -gt 360 ]; then
        keep_all_until=360
    fi

    step=$((interval * 12))
    if [ "$step" -lt 120 ]; then
        step=120
    fi
    if [ "$step" -gt 1440 ]; then
        step=1440
    fi

    filename=$(basename "$file")
    datetime=${filename#db_backup_}
    datetime=${datetime%.sql.zip}

    file_time=$(backup_timestamp_epoch "$datetime") || return 1
    now=$(date +%s)
    age_min=$(((now - file_time) / 60))
    if [ "$age_min" -lt 0 ]; then
        return 1
    fi

    month=${datetime:4:2}
    day=${datetime:6:2}
    hh=${datetime:9:2}
    mm=${datetime:11:2}

    if [ "$age_min" -lt "$keep_all_until" ]; then
        return 0
    fi

    if [ "$age_min" -lt 1440 ]; then
        if is_aligned_minutes "$hh" "$mm" "$step"; then
            return 0
        fi
        return 1
    fi

    if [ "$age_min" -lt $((30 * 1440)) ]; then
        if [ "$hh" = "00" ] && [ "$mm" = "00" ] && [ $((10#$day % 2)) -eq 1 ]; then
            return 0
        fi
        return 1
    fi

    if [ "$age_min" -lt $((180 * 1440)) ]; then
        if [ "$day" = "01" ] && [ "$hh" = "00" ] && [ "$mm" = "00" ]; then
            return 0
        fi
        return 1
    fi

    if [ "$day" = "01" ] && [ "$hh" = "00" ] && [ "$mm" = "00" ] &&
        { [ "$month" = "01" ] || [ "$month" = "07" ]; }; then
        return 0
    fi
    return 1
}

# Files retention driven by CURRENT_FILES_INTERVAL (days):
#   all for files_interval days → every files_interval days until 30d → monthly until 6mo
should_keep_zip_file() {
    local file=$1
    local filename datetime file_time now age_days day
    local interval

    interval=${CURRENT_FILES_INTERVAL:-7}
    if ! [[ "$interval" =~ ^[1-9][0-9]*$ ]]; then
        interval=7
    fi

    filename=$(basename "$file")
    datetime=${filename#files_backup_}
    datetime=${datetime%.zip}

    file_time=$(backup_timestamp_epoch "$datetime") || return 1
    now=$(date +%s)
    age_days=$(((now - file_time) / 86400))
    if [ "$age_days" -lt 0 ]; then
        return 1
    fi

    day=${datetime:6:2}

    if [ "$age_days" -lt "$interval" ]; then
        return 0
    fi

    if [ "$age_days" -lt 30 ]; then
        if [ $(((10#$day - 1) % interval)) -eq 0 ]; then
            return 0
        fi
        return 1
    fi

    if [ "$age_days" -lt 180 ]; then
        if [ "$day" = "01" ]; then
            return 0
        fi
        return 1
    fi

    return 1
}

prune_local_files() {
    local pattern=$1
    local keep_fn=$2
    local file
    local -a files

    shopt -s nullglob
    files=($pattern)
    shopt -u nullglob

    for file in "${files[@]}"; do
        if ! $keep_fn "$file"; then
            echo "Prune local $file"
            rm -f "$file"
        fi
    done
}

# Prune remote copies by listing FTP (catches orphans already gone locally)
prune_remote_files() {
    local folder=$1
    local prefix=$2
    local suffix=$3
    local keep_fn=$4
    local name
    local listing

    if [ -z "$BACKUP_FTP_SERVER" ]; then
        return 0
    fi

    if ! listing=$(ftp_list_names "$folder" 2>/dev/null); then
        echo "FTP list failed for $folder (remote prune skipped)"
        return 0
    fi

    while IFS= read -r name; do
        name="${name#$'\r'}"
        name="${name//$'\r'/}"
        [ -n "$name" ] || continue
        [[ "$name" == "$prefix"* ]] || continue
        [[ "$name" == *"$suffix" ]] || continue

        if ! $keep_fn "$BACKUP_DIR/$folder/$name"; then
            echo "Prune remote $folder/$name"
            ftp_delete "$BACKUP_DIR/$folder/$name" || echo "FTP delete failed: $folder/$name"
        fi
    done <<< "$listing"
}

prune_site_database() {
    local folder=$1
    local site_backup_dir="$BACKUP_DIR/$folder"
    prune_local_files "$site_backup_dir/db_backup_*.sql.zip" should_keep_sql_file
    prune_remote_files "$folder" "db_backup_" ".sql.zip" should_keep_sql_file
}

prune_site_files() {
    local folder=$1
    local site_backup_dir="$BACKUP_DIR/$folder"
    prune_local_files "$site_backup_dir/files_backup_*.zip" should_keep_zip_file
    prune_remote_files "$folder" "files_backup_" ".zip" should_keep_zip_file
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
    rm -f "$db_file"
    rm -f "$site_backup_dir"/db_backup_*.sql

    mark_backup_done "$site_backup_dir/.last_db_backup"

    if [ -n "$BACKUP_FTP_SERVER" ]; then
        ftp_upload "${db_file}.zip" || echo "FTP upload failed: ${db_file}.zip"
    fi
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

    mark_backup_done "$site_backup_dir/.last_files_backup"

    if [ -n "$BACKUP_FTP_SERVER" ]; then
        ftp_upload "$zip_file" || echo "FTP upload failed: $zip_file"
    fi
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
    CURRENT_DB_INTERVAL=$db_interval
    CURRENT_FILES_INTERVAL=$files_interval

    if ! is_disabled "$db_interval"; then
        if [ -z "$database" ] || [ "$database" = "-" ]; then
            echo "Skip database backup for $folder: database name is required"
        elif should_run_interval "$site_backup_dir/.last_db_backup" "$db_interval" minutes; then
            echo "Database backup: $folder ($database), every ${db_interval} minutes"
            backup_site_database "$folder" "$database"
        else
            echo "Skip database backup for $folder: interval ${db_interval} minutes not reached"
        fi
        # Prune local + remote every run (interval only gates creating new backups)
        prune_site_database "$folder"
    else
        echo "Database backup disabled for $folder"
    fi

    if is_disabled "$files_interval"; then
        echo "Files backup disabled for $folder"
        continue
    fi

    if [ "$FILES_CHECK_WINDOW" -eq 0 ]; then
        # Still prune files retention outside the create window
        prune_site_files "$folder"
        continue
    fi

    if [ -z "$paths_csv" ] || [ "$paths_csv" = "-" ]; then
        echo "Skip files backup for $folder: paths are required when files backup is enabled"
        prune_site_files "$folder"
        continue
    fi

    if should_run_interval "$site_backup_dir/.last_files_backup" "$files_interval" days; then
        echo "Files backup: $folder ($paths_csv), every ${files_interval} days"
        backup_site_files "$folder" "$paths_csv" "$ignore_patterns"
    else
        echo "Skip files backup for $folder: interval ${files_interval} days not reached"
    fi
    prune_site_files "$folder"
done < "$SITES_CONF"

sync
