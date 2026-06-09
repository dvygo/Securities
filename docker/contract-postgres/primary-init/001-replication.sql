-- Physical replication user (password must match REPLICATOR_PASSWORD on replica).
CREATE USER replicator WITH REPLICATION LOGIN PASSWORD 'replicator_pass';
