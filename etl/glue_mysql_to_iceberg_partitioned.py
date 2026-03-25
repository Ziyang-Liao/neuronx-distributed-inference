"""
Glue 5.0 ETL: MySQL -> Iceberg partitioned by city (S3).
Incremental via Job Bookmark on updated_at, MERGE INTO for upsert.
"""
import sys
from awsglue.transforms import *
from awsglue.utils import getResolvedOptions
from awsglue.context import GlueContext
from awsglue.job import Job
from pyspark.context import SparkContext
from pyspark.sql.functions import col, sha2, concat, lit, coalesce

args = getResolvedOptions(sys.argv, ['JOB_NAME'])
sc = SparkContext()
glueContext = GlueContext(sc)
spark = glueContext.spark_session
job = Job(glueContext)
job.init(args['JOB_NAME'], args)

# 1. Incremental extract from MySQL (bookmark by updated_at)
datasource = glueContext.create_dynamic_frame.from_catalog(
    database="etl_catalog_db",
    table_name="etl_source_user_data",
    transformation_ctx="datasource_partitioned",
    additional_options={"jobBookmarkKeys": ["updated_at"], "jobBookmarkKeysSortOrder": "asc"}
)

df = datasource.toDF()
if df.count() == 0:
    print("No new/updated records.")
    job.commit()
    sys.exit(0)

count = df.count()
print(f"Extracted {count} incremental records")

# 2. PII masking
df = df.withColumn("username", sha2(col("username"), 256).substr(1, 16))
df = df.withColumn("email", concat(sha2(col("email"), 256).substr(1, 8), lit("@masked.com")))

# 3. Fill null city with 'Unknown'
df = df.withColumn("city", coalesce(col("city"), lit("Unknown")))

# 4. Create partitioned Iceberg table if not exists
spark.sql("""
    CREATE TABLE IF NOT EXISTS glue_catalog.etl_catalog_db.user_data_iceberg_partitioned (
        id BIGINT, username STRING, email STRING, phone STRING,
        address STRING, city STRING, created_at TIMESTAMP, updated_at TIMESTAMP
    ) USING iceberg
    PARTITIONED BY (city)
    LOCATION 's3://etl-pipeline-data-073090110765/iceberg/user_data_partitioned/'
    TBLPROPERTIES ('format-version'='2')
""")

# 5. MERGE INTO: upsert by id
df.createOrReplaceTempView("incremental_data")
spark.sql("""
    MERGE INTO glue_catalog.etl_catalog_db.user_data_iceberg_partitioned t
    USING incremental_data s ON t.id = s.id
    WHEN MATCHED AND s.updated_at > t.updated_at THEN UPDATE SET *
    WHEN NOT MATCHED THEN INSERT *
""")

total = spark.sql("SELECT COUNT(*) FROM glue_catalog.etl_catalog_db.user_data_iceberg_partitioned").collect()[0][0]
print(f"MERGE complete. Incremental: {count}, Total: {total}")

# Show partition distribution
spark.sql("""
    SELECT city, COUNT(*) as cnt
    FROM glue_catalog.etl_catalog_db.user_data_iceberg_partitioned
    GROUP BY city ORDER BY cnt DESC
""").show()

job.commit()
