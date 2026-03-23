"""
Glue 5.0 ETL: MySQL -> S3 Tables (managed Iceberg) with incremental MERGE + PII masking.
Uses AWS analytics integration (GlueCatalog + s3tablescatalog).
S3 Tables provides auto-compaction and snapshot cleanup.
"""
import sys
from awsglue.transforms import *
from awsglue.utils import getResolvedOptions
from awsglue.context import GlueContext
from awsglue.job import Job
from pyspark.context import SparkContext
from pyspark.sql.functions import col, lit, sha2, concat

args = getResolvedOptions(sys.argv, ['JOB_NAME'])
sc = SparkContext()
glueContext = GlueContext(sc)
spark = glueContext.spark_session
job = Job(glueContext)
job.init(args['JOB_NAME'], args)

# --- Config: replace with your values ---
ACCOUNT = "<account-id>"
TABLE_BUCKET = "<your-table-bucket-name>"
NAMESPACE = "etl"
TABLE = "user_data"
# -----------------------------------------

# Configure S3 Tables catalog via AWS analytics integration
spark.conf.set("spark.sql.catalog.s3t", "org.apache.iceberg.spark.SparkCatalog")
spark.conf.set("spark.sql.catalog.s3t.catalog-impl", "org.apache.iceberg.aws.glue.GlueCatalog")
spark.conf.set("spark.sql.catalog.s3t.glue.id", f"{ACCOUNT}:s3tablescatalog/{TABLE_BUCKET}")
spark.conf.set("spark.sql.catalog.s3t.warehouse", f"s3://{TABLE_BUCKET}/warehouse/")

# 1. Incremental extract from MySQL
datasource = glueContext.create_dynamic_frame.from_catalog(
    database="etl_catalog_db", table_name="etl_source_user_data",
    transformation_ctx="datasource",
    additional_options={"jobBookmarkKeys": ["updated_at"], "jobBookmarkKeysSortOrder": "asc"}
)
df = datasource.toDF()
if df.count() == 0:
    print("No new/updated records.")
    job.commit()
    sys.exit(0)

record_count = df.count()
print(f"Extracted {record_count} incremental records")

# 2. PII masking
for col_name in ["username", "email"]:
    if col_name in df.columns:
        if "email" in col_name:
            df = df.withColumn(col_name, concat(sha2(col(col_name), 256).substr(1, 8), lit("@masked.com")))
        else:
            df = df.withColumn(col_name, sha2(col(col_name), 256).substr(1, 16))

# 3. Create table if not exists
spark.sql(f"""
    CREATE TABLE IF NOT EXISTS s3t.{NAMESPACE}.{TABLE} (
        id BIGINT, username STRING, email STRING, phone STRING,
        address STRING, created_at TIMESTAMP, updated_at TIMESTAMP
    ) USING iceberg
""")

# 4. MERGE INTO
df.createOrReplaceTempView("incremental_data")
spark.sql(f"""
    MERGE INTO s3t.{NAMESPACE}.{TABLE} t
    USING incremental_data s ON t.id = s.id
    WHEN MATCHED AND s.updated_at > t.updated_at THEN UPDATE SET *
    WHEN NOT MATCHED THEN INSERT *
""")

total = spark.sql(f"SELECT COUNT(*) FROM s3t.{NAMESPACE}.{TABLE}").collect()[0][0]
print(f"MERGE complete. Incremental: {record_count}, Total in S3 Tables: {total}")
job.commit()
