#!/bin/sh
# Merge base haproxy.cfg + per-site haproxy.cfg (split at ## backend ##).
# Validates on every start; saves backup on success; falls back to backup on failure.
set -eu

BASE="${HAPROXY_BASE_CFG:-/usr/local/etc/haproxy/source/haproxy.cfg}"
OUT="${HAPROXY_RUNTIME_CFG:-/tmp/haproxy.running.cfg}"
BACKUP="${HAPROXY_BACKUP_CFG:-/var/lib/haproxy/assembled.cfg.bak}"
SITES="${SITES_DIR:-/sites}"
NEW="/tmp/haproxy.assembled.new"
FRONT_PART=$(mktemp)
BACK_PART=$(mktemp)
CHECK_ERR=$(mktemp)

log() {
    echo "[assemble-config] $*" >&2
}

on_exit() {
    rc=$?
    rm -f "$FRONT_PART" "$BACK_PART" "$CHECK_ERR"
    if [ "$rc" -ne 0 ]; then
        log "ERROR: exited with status $rc"
    fi
}
trap on_exit EXIT

validate_cfg() {
    cfg=$1
    log "Validating $cfg ..."
    if command -v timeout >/dev/null 2>&1; then
        if timeout 30 /usr/local/sbin/haproxy -c -f "$cfg" >"$CHECK_ERR" 2>&1; then
            return 0
        fi
    elif /usr/local/sbin/haproxy -c -f "$cfg" >"$CHECK_ERR" 2>&1; then
        return 0
    fi
    log "Config check failed:"
    cat "$CHECK_ERR" >&2
    return 1
}

start_haproxy() {
    cfg=$1
    log "Starting HAProxy with $cfg"
    exec /usr/local/sbin/haproxy -W -db -f "$cfg"
}

finalize_cfg() {
    cfg=$1
    tmp="${cfg}.final"
    awk '
        /^[[:space:]]*server / {
            line = $0
            if (line !~ /init-addr/) line = line " init-addr none"
            if (line !~ /resolvers/) line = line " resolvers docker"
            print line
            next
        }
        { print }
    ' "$cfg" > "$tmp"
    mv "$tmp" "$cfg"
}

split_front() {
    sed 's/\r$//' "$1" | sed '/^## backend ##/,$d'
}

split_back() {
    sed 's/\r$//' "$1" | sed -n '/^## backend ##/,$p' | sed '1d'
}

append_site() {
    site_cfg=$1
    log "  include $site_cfg"
    split_front "$site_cfg" >> "$FRONT_PART"
    split_back "$site_cfg" >> "$BACK_PART"
}

if [ ! -f "$BASE" ]; then
    log "ERROR: Base config not found: $BASE"
    exit 1
fi

mkdir -p "$(dirname "$BACKUP")"

: > "$FRONT_PART"
: > "$BACK_PART"

log "Assembling config from $SITES"

if [ -f "${SITES}/_admintools/haproxy.cfg" ]; then
    append_site "${SITES}/_admintools/haproxy.cfg"
fi

for site_dir in "$SITES"/*/; do
    [ -d "$site_dir" ] || continue
    case "$site_dir" in
        */_template_wordpress/|*/_template_static/|*/_admintools/) continue ;;
    esac
    site_cfg="${site_dir}haproxy.cfg"
    [ -f "$site_cfg" ] || continue
    append_site "$site_cfg"
done

if ! {
    sed 's/\r$//' "$BASE" | sed '/# @@INCLUDE_FRONT@@/,$d'
    cat "$FRONT_PART"
    sed 's/\r$//' "$BASE" | sed -n '/# @@INCLUDE_FRONT@@/,/# @@INCLUDE_BACK@@/p' | sed '1d;$d'
    cat "$BACK_PART"
    sed 's/\r$//' "$BASE" | sed -n '/# @@INCLUDE_BACK@@/,$p' | tail -n +2
} > "$NEW"; then
    log "ERROR: Failed to merge config files"
    exit 1
fi

bytes=$(wc -c < "$NEW" | tr -d ' ')
finalize_cfg "$NEW"
log "Merged config written ($bytes bytes)"

if validate_cfg "$NEW"; then
    cp "$NEW" "$OUT"
    cp "$NEW" "$BACKUP"
    log "OK: validated; backup saved to $BACKUP"
    start_haproxy "$OUT"
fi

log "Assembled config is invalid; checking for previous backup at $BACKUP"

if [ ! -f "$BACKUP" ]; then
    log "ERROR: No previous successful config backup found — fix site haproxy.cfg and restart haproxy"
    exit 1
fi

if validate_cfg "$BACKUP"; then
    cp "$BACKUP" "$OUT"
    log "WARN: Using previous backup — fix site haproxy.cfg and restart haproxy"
    start_haproxy "$OUT"
fi

log "ERROR: Previous backup exists but is invalid — fix site haproxy.cfg and restart haproxy"
exit 1
