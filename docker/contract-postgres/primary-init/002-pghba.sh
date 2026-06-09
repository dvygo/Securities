#!/usr/bin/env bash
set -e
{
  echo ""
  echo "# contract-postgres: clients + streaming replica"
  echo "host all all 0.0.0.0/0 scram-sha-256"
  echo "host replication replicator 0.0.0.0/0 scram-sha-256"
} >> "${PGDATA}/pg_hba.conf"
