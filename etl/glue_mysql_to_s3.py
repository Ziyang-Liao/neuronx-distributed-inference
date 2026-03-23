"""
Glue ETL Job: MySQL -> S3 (incremental by updated_at + PII masking)
Writes to unique S3 prefix per run, passes path to downstream via Workflow run properties.
"""
import sys
import boto3
from awsglue.transforms import *
from awsglue.utils import getResolvedOptions
from awsglue.context import GlueContext
from awsglue.job import Job
from pyspark.context import SparkContext
from pyspark.sql.functions import col, lit, sha2, concat

args = getResolvedOptions(sys.argv, [
    'JOB_NAME', 'source_database', 'source_table',
    'target_s3_path', 'connection_name', 'mask_columns',
    'WORKFLOW_NAME', 'WORKFLOW_RUN_ID'
])

sc = SparkContext()
glueContext = GlueContext(sc)
spark = glueContext.spark_session
job = Job(glueContext)
job.init(args['JOB_NAME'], args)

datasource = glueContext.create_dynamic_frame.from_catalog(
    database=args['source_database'],
    table_name=args['source_table'],
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

for col_name in [c.strip() for c in args['mask_columns'].split(',') if c.strip()]:
    if col_name in df.columns:
        if 'email' in col_name.lower():
            df = df.withColumn(col_name, concat(sha2(col(col_name), 256).substr(1, 8), lit("@masked.com")))
        else:
            df = df.withColumn(col_name, sha2(col(col_name), 256).substr(1, 16))

# Write to unique prefix per workflow run
run_id = args['WORKFLOW_RUN_ID']
output_path = f"{args['target_s3_path']}/incremental/{run_id}/"
df.coalesce(1).write.mode("overwrite").parquet(output_path)
print(f"Wrote {df.count()} records to {output_path}")

# Pass output path to downstream Job2 via workflow run properties
glue = boto3.client('glue', region_name='us-east-1')
glue.put_workflow_run_properties(
    Name=args['WORKFLOW_NAME'],
    RunId=args['WORKFLOW_RUN_ID'],
    RunProperties={'incremental_s3_path': output_path}
)

job.commit()
