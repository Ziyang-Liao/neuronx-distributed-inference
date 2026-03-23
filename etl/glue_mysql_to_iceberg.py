"""
Glue 5.0 ETL: MySQL -> Iceberg (S3) with incremental MERGE + PII masking.
Uses glue_catalog configured via --conf job parameter.
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

# 1. Incremental extract from MySQL
datasource = glueContext.create_dynamic_frame.from_catalog(
    database="etl_catalog_db",
    table_name="etl_source_user_data",
    transformation_ctx="datasource",
    additional_options={
        "jobBookmarkKeys": ["updated_at"],
        "jobBookmarkKeysSortOrder": "asc"
    }
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

# 3. Create Iceberg table if not exists
spark.sql("""
    CREATE TABLE IF NOT EXISTS glue_catalog.etl_catalog_db.user_data_iceberg (
        id BIGINT, username STRING, email STRING, phone STRING,
        address STRING, created_at TIMESTAMP, updated_at TIMESTAMP
    ) USING iceberg
    LOCATION 's3://etl-pipeline-data-073090110765/iceberg/user_data/'
    TBLPROPERTIES ('format-version'='2')
""")

# 4. MERGE INTO: incremental upsert + dedup
df.createOrReplaceTempView("incremental_data")
spark.sql("""
    MERGE INTO glue_catalog.etl_catalog_db.user_data_iceberg t
    USING incremental_data s ON t.id = s.id
    WHEN MATCHED AND s.updated_at > t.updated_at THEN UPDATE SET *
    WHEN NOT MATCHED THEN INSERT *
""")

total = spark.sql("SELECT COUNT(*) FROM glue_catalog.etl_catalog_db.user_data_iceberg").collect()[0][0]
print(f"MERGE complete. Incremental: {record_count}, Total in Iceberg: {total}")

job.commit()
