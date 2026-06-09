#!/bin/sh
set -eu

PRIMARY_HOST="${PRIMARY_HOST:-contract-primary}"
REPL_USER="${REPLICATOR_USER:-replicator}"
REPL_PASS="${REPLICATOR_PASSWORD:-replicator_pass}"
SLOT="${REPLICATION_SLOT:-contract_replica_1}"
PGSU_USER="${POSTGRES_USER:-cuser}"
PGSU_PASS="${POSTGRES_PASSWORD:-}"

export PGPASSWORD="${REPL_PASS}"

if [ ! -f "${PGDATA}/PG_VERSION" ]; then
  echo "contract-replica: waiting for primary ${PRIMARY_HOST}:5432 ..."
  until pg_isready -h "${PRIMARY_HOST}" -p 5432 -q; do
    sleep 2
  done
  rm -rf "${PGDATA:?}/"*
  slot_count=""
  if [ -n "${PGSU_PASS}" ]; then
    slot_raw=$(PGPASSWORD="${PGSU_PASS}" psql -h "${PRIMARY_HOST}" -p 5432 -U "${PGSU_USER}" -d postgres \
      -v ON_ERROR_STOP=1 -tAc "SELECT count(*)::text FROM pg_replication_slots WHERE slot_name = '${SLOT}'")
    slot_count=$(printf '%s' "$slot_raw" | tr -d '[:space:]')
  fi
  if [ "${slot_count}" = "0" ] || [ -z "${slot_count}" ]; then
    echo "contract-replica: pg_basebackup from ${PRIMARY_HOST} create slot=${SLOT} ..."
    pg_basebackup \
      -h "${PRIMARY_HOST}" -p 5432 \
      -U "${REPL_USER}" \
      -D "${PGDATA}" \
      -Fp -Xs -P -R \
      -C -S "${SLOT}"
  else
    echo "contract-replica: pg_basebackup from ${PRIMARY_HOST} reuse slot=${SLOT} ..."
    pg_basebackup \
      -h "${PRIMARY_HOST}" -p 5432 \
      -U "${REPL_USER}" \
      -D "${PGDATA}" \
      -Fp -Xs -P -R \
      -S "${SLOT}"
  fi
  chown -R postgres:postgres "${PGDATA}" 2>/dev/null || chown -R 70:70 "${PGDATA}" 2>/dev/null || true
fi

exec /usr/local/bin/docker-entrypoint.sh postgres -c listen_addresses='*' "$@"
