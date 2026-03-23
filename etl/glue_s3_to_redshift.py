"""
Glue Python Shell: S3 -> Redshift (incremental COPY + MERGE)
Only loads today's partition, not full history.
TEMP TABLE + COPY + DEDUP + MERGE in same session.
"""
import redshift_connector
from datetime import datetime

today = datetime.utcnow().strftime('%Y-%m-%d')
s3_incremental_path = f's3://etl-pipeline-data-073090110765/data/masked/dt={today}/'

conn = redshift_connector.connect(
    host='etl-redshift.cnib5syzt6zq.us-east-1.redshift.amazonaws.com',
    port=5439, database='etl_dw', user='admin', password='Admin123!'
)
conn.autocommit = True
cur = conn.cursor()

sqls = [
    # 1. Temp staging table
    "CREATE TEMP TABLE staging (LIKE public.user_data)",

    # 2. COPY only today's incremental partition
    f"COPY staging FROM '{s3_incremental_path}' IAM_ROLE 'arn:aws:iam::073090110765:role/etl-redshift-s3-role' FORMAT AS PARQUET",

    # 3. Dedup within incremental batch (same id may appear multiple times in one batch)
    """CREATE TEMP TABLE staging_deduped AS
       SELECT * FROM (
           SELECT *, ROW_NUMBER() OVER (PARTITION BY id ORDER BY updated_at DESC) rn
           FROM staging
       ) WHERE rn = 1""",

    "DROP TABLE staging",
    "ALTER TABLE staging_deduped RENAME TO staging",

    # 4. MERGE: only compares incremental ids against target, not full table scan
    #    Redshift uses SORTKEY(id) to efficiently locate matching rows
    """MERGE INTO public.user_data USING staging ON public.user_data.id = staging.id
       WHEN MATCHED THEN UPDATE SET
           username=staging.username, email=staging.email, phone=staging.phone,
           address=staging.address, created_at=staging.created_at, updated_at=staging.updated_at
       WHEN NOT MATCHED THEN INSERT
           VALUES (staging.id, staging.username, staging.email, staging.phone,
                   staging.address, staging.created_at, staging.updated_at)"""
]

for i, sql in enumerate(sqls, 1):
    print(f"Step {i}: {sql[:80]}...")
    cur.execute(sql)
    print(f"Step {i}: OK")

# Report
cur.execute("SELECT COUNT(*) FROM public.user_data")
total = cur.fetchone()[0]
print(f"Done. Incremental path: {s3_incremental_path}")
print(f"Final user_data rows: {total}")

cur.close()
conn.close()
