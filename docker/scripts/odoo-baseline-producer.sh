#!/usr/bin/env bash
set -euo pipefail

: "${PGDATA:?}"
: "${POSTGRES_USER:?}"
: "${BASELINE_DATABASE:?}"
: "${BASELINE_POSTGRES_VERSION:?}"
: "${BASELINE_ARCHITECTURE:?}"
: "${BASELINE_BINDING_SHA256:?}"
: "${BASELINE_DUMP_SHA256:?}"
: "${BASELINE_FILESTORE_SHA256:?}"
: "${BASELINE_DATABASE_IDENTITY:?}"
: "${BASELINE_SETTINGS:?}"
[[ "$BASELINE_DATABASE" =~ ^[A-Za-z0-9_]+$ ]]
test -z "$(find "$PGDATA" -mindepth 1 -print -quit)"
cd /workspace/baseline/input
mkdir /workspace/baseline/results
test "$(postgres --version)" = "$BASELINE_POSTGRES_VERSION"
test "$(uname -m)" = "$BASELINE_ARCHITECTURE"
printf '%s  %s\n' "$BASELINE_BINDING_SHA256" binding.json "$BASELINE_DUMP_SHA256" database.dump | sha256sum --check --status
test -z "$(find filestore ! -type f ! -type d -print -quit)"
find filestore -type f -exec sha256sum {} + | LC_ALL=C sort > /workspace/baseline/results/filestore.sha256
printf '%s  %s\n' "$BASELINE_FILESTORE_SHA256" /workspace/baseline/results/filestore.sha256 | sha256sum --check --status

stop_postgres() {
    if test -f "$PGDATA/postmaster.pid"; then
        gosu postgres pg_ctl -D "$PGDATA" -m fast -w stop
    fi
}
trap stop_postgres EXIT
started=$(date +%s%N)
docker-entrypoint.sh postgres &
server=$!
for attempt in {1..60}; do
    if pg_isready -h 127.0.0.1 -U "$POSTGRES_USER"; then
        break
    fi
    kill -0 "$server"
    sleep 1
done
pg_isready -h 127.0.0.1 -U "$POSTGRES_USER"
restore_started=$(date +%s%N)
pg_restore -h 127.0.0.1 -U "$POSTGRES_USER" --exit-on-error --create --dbname postgres database.dump
printf '%s\n' "$(( $(date +%s%N) - restore_started ))" > /workspace/baseline/results/restore-nanoseconds.txt
psql -X -h 127.0.0.1 -U "$POSTGRES_USER" -d "$BASELINE_DATABASE" -At -v ON_ERROR_STOP=1 \
    -c "SELECT pg_encoding_to_char(encoding), datcollate, datctype, datlocprovider FROM pg_database WHERE datname = current_database()" \
    > /workspace/baseline/results/database-identity.txt
test "$(cat /workspace/baseline/results/database-identity.txt)" = "$BASELINE_DATABASE_IDENTITY"
psql -X -h 127.0.0.1 -U "$POSTGRES_USER" -d "$BASELINE_DATABASE" -At -v ON_ERROR_STOP=1 \
    -c "SELECT name, setting FROM pg_settings WHERE name IN ('lc_messages','lc_monetary','lc_numeric','lc_time','default_text_search_config','TimeZone') ORDER BY name COLLATE \"C\"" \
    > /workspace/baseline/results/settings.txt
test "$(cat /workspace/baseline/results/settings.txt)" = "$BASELINE_SETTINGS"
stop_postgres
wait "$server"
test ! -e "$PGDATA/postmaster.pid"
LC_ALL=C pg_controldata "$PGDATA" > /workspace/baseline/results/control.txt
test "$(sed -n 's/^Database cluster state: *//p' /workspace/baseline/results/control.txt)" = 'shut down'
test -z "$(find "$PGDATA/pg_tblspc" -mindepth 1 -print -quit)"
test -z "$(find "$PGDATA" filestore ! -type f ! -type d -print -quit)"
for marker in standby.signal recovery.signal recovery.conf backup_label tablespace_map; do
    test ! -e "$PGDATA/$marker"
done
for directory in "$PGDATA" "$PGDATA/pg_wal" filestore; do
    du -sb "$directory"
done > /workspace/baseline/results/expanded-bytes.txt
postgres --version > /workspace/baseline/results/postgres-version.txt
tar --format=ustar --create --file=/workspace/baseline/results/stage.tar \
    --transform='s,^\.,pgdata,' --directory="$PGDATA" . \
    --directory=/workspace/baseline/input filestore binding.json
cd /workspace/baseline/results
sha256sum stage.tar > stage.sha256
printf '%s\n' "$(( $(date +%s%N) - started ))" > creation-nanoseconds.txt
