#!/usr/bin/env bash
set -euo pipefail
export LC_ALL=C

: "${PGDATA:?}"
: "${POSTGRES_USER:?}"
: "${BASELINE_DATABASE:?}"
[[ "$BASELINE_DATABASE" =~ ^[A-Za-z0-9_]+$ ]]
test -z "$(find "$PGDATA" -mindepth 1 -print -quit)"
cd /workspace/baseline/input
sha256sum --check inputs.sha256
mkdir /workspace/baseline/results

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
createdb -h 127.0.0.1 -U "$POSTGRES_USER" "$BASELINE_DATABASE"
pg_restore -h 127.0.0.1 -U "$POSTGRES_USER" --exit-on-error --dbname "$BASELINE_DATABASE" database.dump
stop_postgres
wait "$server"
test ! -e "$PGDATA/postmaster.pid"
pg_controldata "$PGDATA" > /workspace/baseline/results/control.txt
test "$(sed -n 's/^Database cluster state: *//p' /workspace/baseline/results/control.txt)" = 'shut down'
test -z "$(find "$PGDATA/pg_tblspc" -mindepth 1 -print -quit)"
test -z "$(find "$PGDATA" filestore ! -type f ! -type d -print -quit)"
for marker in standby.signal recovery.signal recovery.conf backup_label tablespace_map; do
    test ! -e "$PGDATA/$marker"
done
du -sb "$PGDATA" "$PGDATA/pg_wal" filestore > /workspace/baseline/results/expanded-bytes.txt
postgres --version > /workspace/baseline/results/postgres-version.txt
tar --format=ustar --create --file=/workspace/baseline/results/stage.tar \
    --transform='s,^\.,pgdata,' --directory="$PGDATA" . \
    --directory=/workspace/baseline/input filestore binding.json
cd /workspace/baseline/results
sha256sum stage.tar > stage.sha256
printf '%s\n' "$(( $(date +%s%N) - started ))" > creation-nanoseconds.txt
