#!/usr/bin/env bash
# Nightly pg_dump with rotation. Runs inside the postgres:16 image (same version as
# the server, which pg_dump requires). One dump right away if today's is missing,
# then one every day at $BACKUP_AT (container TZ).
set -u
DIR=${BACKUP_DIR:-/backups}
mkdir -p "$DIR"

dump() {
  local day out tmp
  day=$(date +%F)
  out="$DIR/driftwatch-$day.dump"
  tmp="$out.partial"
  echo "$(date '+%F %T') backup: starting $out"
  # bt_* tables are rebuildable from public data (backtest-load): schema only, no rows.
  if pg_dump --format=custom --compress=6 --exclude-table-data='bt_*' --file="$tmp" \
     && pg_restore --list "$tmp" > /dev/null; then       # proves the file is readable
    mv -f "$tmp" "$out"
    echo "$(date '+%F %T') backup: ok $(du -h "$out" | cut -f1)"
    find "$DIR" -name 'driftwatch-*.dump' -mtime +"${KEEP_DAYS:-14}" -print -delete
  else
    rm -f "$tmp"
    echo "$(date '+%F %T') backup: FAILED" >&2
    psql -qc "INSERT INTO notifications (kind, text) VALUES \
      ('alert', '🚨 Nightly database backup FAILED. Check: docker compose logs backup')" \
      || true
  fi
}

until pg_isready -q; do sleep 5; done
[ -f "$DIR/driftwatch-$(date +%F).dump" ] || dump
while true; do
  now=$(date +%s)
  next=$(date -d "today ${BACKUP_AT:-02:30}" +%s)
  [ "$next" -le "$now" ] && next=$(date -d "tomorrow ${BACKUP_AT:-02:30}" +%s)
  sleep $((next - now))
  dump
done
