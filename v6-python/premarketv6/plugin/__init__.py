"""The plugin format: the legacy pg symbol-master schema this pipeline also emits.

Everything plugin-shaped lives here rather than beside the canonical normalizer,
because the two answer to different owners. normalize/ produces our schema, whose
columns we choose; plugin/ produces someone else's, whose columns and types are
fixed by docs/plugin/pg_data_types.txt and whose table DDL lives outside this
repo. Keeping them apart means a change to our schema cannot silently reshape
what the plugin pushes, and the reverse.

  build.py     normalized parquet -> plugin parquet in data/YYYYMMDD/v6/plugin/
  postgres.py  that plugin parquet -> the external Postgres table, upserted on
               (token, trade_date)

Import the submodule you want -- `from .plugin import build, postgres`. Nothing
is re-exported here on purpose: postgres.py pulls in psycopg, and a build-only
caller should not have to.
"""
