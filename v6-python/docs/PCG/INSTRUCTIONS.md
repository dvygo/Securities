To: Production Control Group

1. ```source .venv/bin/activate```

2. ```python -m premarketv6 {india, xnas, xcme, xcbo}```

3. from NSE contract file 'NEW FILE FORMAT.rar', extract entire folder as a
   folder to /{YYYYMMDD}/XNSE/*

   Skip this while conf/config.ini has `[EXCHANGE:XNSE] feed = fyers` --
   step 2's `india` download already supplies XNSE. It applies only when
   XNSE is switched back to the NSE file drop.

4. ```python -m premarketv6 normalize```

5. ```python -m premarketv6 plugin```

   Runs normalize first, then, in order: the plugin Parquet, the Postgres
   push, and MDF's tokenmap.<VENUE>.bin. Step 4 is therefore optional
   before this -- run it alone when you want the normalized tree and the
   ClickHouse push without any plugin output.

   To redo one stage without the others (each still re-runs normalize):

     python -m premarketv6 plugin --parquet-only
     python -m premarketv6 plugin --postgres-push-only
     python -m premarketv6 plugin --tokenmap-only

   All output for the day lands under data/{YYYYMMDD}/TRANSFORM/.
