# RDS → Glue → S3 Table (Iceberg) → Redshift Workshop

## Architecture

```
RDS MySQL (private)
    │
    ▼ [Glue 5.0 ETL Job - incremental by updated_at]
    │  ① Bookmark 增量抽取 (只读新增/更新的记录)
    │  ② PII 脱敏: username → SHA256, email → hash@masked.com
    │  ③ MERGE INTO Iceberg (自动去重, 按 id upsert)
    │
S3 Iceberg Table (自动管理文件, 无需手动分区)
    │
    ▼ [Redshift Spectrum 直读, 零 COPY]
Redshift
    └── iceberg_schema.user_data_iceberg (外部表)
    └── v_user_dimension (维度计算视图)
```

## Why Iceberg?

| 对比项 | Parquet + COPY 方案 | Iceberg MERGE 方案 |
|--------|--------------------|--------------------|
| Glue Jobs | 2 个 | **1 个** |
| S3 文件管理 | 手动管理分区/路径 | **Iceberg 自动管理** |
| 去重 | TEMP TABLE + DEDUP + MERGE | **MERGE INTO 一条 SQL** |
| Redshift 加载 | COPY + staging 表 | **Spectrum 直读, 零加载** |
| 增量控制 | Bookmark + 文件路径传递 | **Bookmark + MERGE ON id** |

---

## Prerequisites

- AWS Account with permissions for VPC, RDS, Redshift, Glue, S3, IAM, Lake Formation
- AWS CLI configured
- Region: `us-east-1`

---

## Step 1: Network (VPC + Private Subnets)

All resources must be in private subnets, no public access.

```bash
# Create private subnets in existing VPC (or create new VPC)
VPC_ID="vpc-07288bbb688c103a8"  # your VPC

aws ec2 create-subnet --vpc-id $VPC_ID --cidr-block 13.14.10.0/24 \
  --availability-zone us-east-1a \
  --tag-specifications 'ResourceType=subnet,Tags=[{Key=Name,Value=etl-private-subnet-1a}]'

aws ec2 create-subnet --vpc-id $VPC_ID --cidr-block 13.14.11.0/24 \
  --availability-zone us-east-1b \
  --tag-specifications 'ResourceType=subnet,Tags=[{Key=Name,Value=etl-private-subnet-1b}]'

# Associate with NAT Gateway route table (Glue needs outbound)
aws ec2 associate-route-table --route-table-id rtb-xxx --subnet-id subnet-xxx

# Create Security Group (VPC CIDR only, NO 0.0.0.0/0)
aws ec2 create-security-group --group-name etl-pipeline-sg \
  --description "ETL pipeline - internal only" --vpc-id $VPC_ID

SG_ID="sg-xxx"
aws ec2 authorize-security-group-ingress --group-id $SG_ID --protocol tcp --port 3306 --cidr 13.14.0.0/16
aws ec2 authorize-security-group-ingress --group-id $SG_ID --protocol tcp --port 5439 --cidr 13.14.0.0/16
aws ec2 authorize-security-group-ingress --group-id $SG_ID --protocol tcp --port 443 --cidr 13.14.0.0/16
aws ec2 authorize-security-group-ingress --group-id $SG_ID --protocol -1 --source-group $SG_ID

# S3 VPC Gateway Endpoint
aws ec2 create-vpc-endpoint --vpc-id $VPC_ID --service-name com.amazonaws.us-east-1.s3 \
  --vpc-endpoint-type Gateway --route-table-ids rtb-xxx
```

## Step 2: RDS MySQL (Private)

```bash
# Subnet group
aws rds create-db-subnet-group --db-subnet-group-name etl-db-subnet-group \
  --db-subnet-group-description "ETL private subnets" \
  --subnet-ids subnet-1a subnet-1b

# Create MySQL (no public access)
aws rds create-db-instance \
  --db-instance-identifier etl-mysql \
  --db-instance-class db.t3.micro \
  --engine mysql --engine-version 8.0 \
  --master-username admin --master-user-password admin123 \
  --db-name etl_source \
  --db-subnet-group-name etl-db-subnet-group \
  --vpc-security-group-ids $SG_ID \
  --allocated-storage 20 --storage-type gp3 \
  --no-publicly-accessible
```

## Step 3: Redshift (Private)

```bash
# Subnet group
aws redshift create-cluster-subnet-group --cluster-subnet-group-name etl-redshift-subnet-group \
  --description "ETL Redshift private subnets" --subnet-ids subnet-1a subnet-1b

# Create cluster (no public access)
aws redshift create-cluster \
  --cluster-identifier etl-redshift \
  --node-type ra3.xlplus --cluster-type single-node \
  --master-username admin --master-user-password 'Admin123!' \
  --db-name etl_dw \
  --cluster-subnet-group-name etl-redshift-subnet-group \
  --vpc-security-group-ids $SG_ID \
  --no-publicly-accessible
```

## Step 4: S3 Bucket

```bash
aws s3api create-bucket --bucket etl-pipeline-data-${ACCOUNT_ID}

# Block ALL public access
aws s3api put-public-access-block --bucket etl-pipeline-data-${ACCOUNT_ID} \
  --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
```

## Step 5: IAM Roles

```bash
# Glue Role
aws iam create-role --role-name etl-glue-role \
  --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"glue.amazonaws.com"},"Action":"sts:AssumeRole"}]}'

aws iam attach-role-policy --role-name etl-glue-role \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSGlueServiceRole

# Glue inline policy (S3 + Glue Catalog + Lake Formation)
aws iam put-role-policy --role-name etl-glue-role --policy-name etl-glue-s3tables-policy \
  --policy-document '{
    "Version":"2012-10-17",
    "Statement":[
      {"Effect":"Allow","Action":["s3:*Object*","s3:ListBucket","s3:GetBucketLocation"],"Resource":"*"},
      {"Effect":"Allow","Action":["glue:*Database*","glue:*Table*","glue:*Partition*","glue:*Catalog*"],"Resource":"*"},
      {"Effect":"Allow","Action":["lakeformation:GetDataAccess"],"Resource":"*"},
      {"Effect":"Allow","Action":["logs:*"],"Resource":"*"}
    ]
  }'

# Redshift Role (for Spectrum to read S3 + Glue Catalog)
aws iam create-role --role-name etl-redshift-s3-role \
  --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"redshift.amazonaws.com"},"Action":"sts:AssumeRole"}]}'

aws iam put-role-policy --role-name etl-redshift-s3-role --policy-name etl-redshift-s3-read \
  --policy-document '{
    "Version":"2012-10-17",
    "Statement":[
      {"Effect":"Allow","Action":["s3:GetObject","s3:ListBucket","s3:GetBucketLocation"],"Resource":"*"},
      {"Effect":"Allow","Action":["glue:*Database*","glue:*Table*","glue:*Partition*","glue:*Catalog*"],"Resource":"*"},
      {"Effect":"Allow","Action":["lakeformation:GetDataAccess"],"Resource":"*"}
    ]
  }'

# Associate Redshift role
aws redshift modify-cluster-iam-roles --cluster-identifier etl-redshift \
  --add-iam-roles arn:aws:iam::${ACCOUNT_ID}:role/etl-redshift-s3-role
```

## Step 6: Lake Formation Permissions

```bash
# Grant Glue role access to catalog database
aws lakeformation grant-permissions \
  --principal '{"DataLakePrincipalIdentifier":"arn:aws:iam::ACCOUNT:role/etl-glue-role"}' \
  --resource '{"Database":{"Name":"etl_catalog_db"}}' --permissions ALL

aws lakeformation grant-permissions \
  --principal '{"DataLakePrincipalIdentifier":"arn:aws:iam::ACCOUNT:role/etl-glue-role"}' \
  --resource '{"Table":{"DatabaseName":"etl_catalog_db","TableWildcard":{}}}' --permissions ALL

# Same for Redshift role
aws lakeformation grant-permissions \
  --principal '{"DataLakePrincipalIdentifier":"arn:aws:iam::ACCOUNT:role/etl-redshift-s3-role"}' \
  --resource '{"Database":{"Name":"etl_catalog_db"}}' --permissions ALL

aws lakeformation grant-permissions \
  --principal '{"DataLakePrincipalIdentifier":"arn:aws:iam::ACCOUNT:role/etl-redshift-s3-role"}' \
  --resource '{"Table":{"DatabaseName":"etl_catalog_db","TableWildcard":{}}}' --permissions ALL
```

## Step 7: Glue Connections + Crawler

```bash
# Glue Catalog Database
aws glue create-database --database-input '{"Name":"etl_catalog_db"}'

# MySQL JDBC Connection
aws glue create-connection --connection-input '{
  "Name":"etl-mysql-connection",
  "ConnectionType":"JDBC",
  "ConnectionProperties":{
    "JDBC_CONNECTION_URL":"jdbc:mysql://YOUR_RDS_ENDPOINT:3306/etl_source",
    "USERNAME":"admin","PASSWORD":"admin123"
  },
  "PhysicalConnectionRequirements":{
    "SubnetId":"subnet-1a",
    "SecurityGroupIdList":["sg-xxx"],
    "AvailabilityZone":"us-east-1a"
  }
}'

# Crawler (crawls MySQL table metadata into Glue Catalog)
aws glue create-crawler --name etl-mysql-crawler \
  --role arn:aws:iam::ACCOUNT:role/etl-glue-role \
  --database-name etl_catalog_db \
  --targets '{"JdbcTargets":[{"ConnectionName":"etl-mysql-connection","Path":"etl_source/%"}]}'
```

## Step 8: Initialize MySQL Test Data

Use a Glue Python Shell job to insert data (since MySQL is in private subnet):

```python
# glue_init_mysql.py
import pymysql

conn = pymysql.connect(host='YOUR_RDS_ENDPOINT', port=3306,
                       user='admin', password='admin123', database='etl_source')
cur = conn.cursor()

cur.execute("""CREATE TABLE IF NOT EXISTS user_data (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    username VARCHAR(256) NOT NULL,
    email VARCHAR(256) NOT NULL,
    phone VARCHAR(64),
    address VARCHAR(512),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_updated_at (updated_at)
)""")

data = [
    ('john_doe','john@example.com','555-0101','123 Main St, Springfield, IL'),
    ('jane_smith','jane@example.com','555-0102','456 Oak Ave, Portland, OR'),
    ('bob_wilson','bob@example.com','555-0103','789 Pine Rd, Austin, TX'),
    ('alice_chen','alice@example.com','555-0104','321 Elm St, Seattle, WA'),
    ('charlie_brown','charlie@example.com','555-0105','654 Maple Dr, Denver, CO'),
]
cur.executemany("INSERT INTO user_data (username,email,phone,address) VALUES (%s,%s,%s,%s)", data)
conn.commit()
cur.close()
conn.close()
```

```bash
# Run crawler to register MySQL metadata in Glue Catalog
aws glue start-crawler --name etl-mysql-crawler
```

## Step 9: Glue 5.0 ETL Job (Core - One Job Does Everything)

### Job Script: `glue_mysql_to_iceberg.py`

```python
"""
Glue 5.0 ETL: MySQL -> Iceberg with incremental MERGE + PII masking.
One job: incremental extract → mask → MERGE INTO Iceberg (auto dedup).
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

# 1. Incremental extract from MySQL (bookmark by updated_at)
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
    LOCATION 's3://YOUR_BUCKET/iceberg/user_data/'
    TBLPROPERTIES ('format-version'='2')
""")

# 4. MERGE INTO: incremental upsert + auto dedup
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
```

### Create & Configure Job

```bash
# Upload script
aws s3 cp glue_mysql_to_iceberg.py s3://YOUR_BUCKET/scripts/

# Create Glue 5.0 Job
aws glue create-job \
  --name etl-mysql-to-iceberg \
  --role arn:aws:iam::ACCOUNT:role/etl-glue-role \
  --glue-version 5.0 \
  --worker-type G.1X --number-of-workers 2 \
  --connections '{"Connections":["etl-mysql-connection"]}' \
  --command '{"Name":"glueetl","ScriptLocation":"s3://YOUR_BUCKET/scripts/glue_mysql_to_iceberg.py","PythonVersion":"3"}' \
  --default-arguments '{
    "--job-bookmark-option":"job-bookmark-enable",
    "--datalake-formats":"iceberg",
    "--conf":"spark.sql.extensions=org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions --conf spark.sql.catalog.glue_catalog=org.apache.iceberg.spark.SparkCatalog --conf spark.sql.catalog.glue_catalog.warehouse=s3://YOUR_BUCKET/iceberg/ --conf spark.sql.catalog.glue_catalog.catalog-impl=org.apache.iceberg.aws.glue.GlueCatalog --conf spark.sql.catalog.glue_catalog.io-impl=org.apache.iceberg.aws.s3.S3FileIO"
  }'
```

### Key Configuration Explained

| Parameter | Purpose |
|-----------|---------|
| `--datalake-formats iceberg` | Load Iceberg JARs |
| `--job-bookmark-option job-bookmark-enable` | Enable incremental extraction |
| `jobBookmarkKeys: ["updated_at"]` | Track by updated_at to capture INSERT + UPDATE |
| `MERGE INTO ... WHEN MATCHED AND s.updated_at > t.updated_at` | Only update if source is newer |
| `TBLPROPERTIES ('format-version'='2')` | Iceberg v2 supports row-level MERGE |

### ⚠️ 增量抽取原理 (重要)

增量的核心是 **Glue Job Bookmark**，它记录每次运行读到的 `updated_at` 最大值，下次只读大于该值的记录。

```
第1次运行: SELECT * FROM user_data → 读取全部 12 条
           bookmark 记录 max(updated_at) = 2026-03-23 16:08:00

第2次运行: SELECT * FROM user_data WHERE updated_at > '2026-03-23 16:08:00'
           → 只读取 2 条新增/更新的记录
           bookmark 更新 max(updated_at) = 2026-03-23 17:30:00

第3次运行: 如果没有新数据 → 读取 0 条, 直接退出
```

**为什么用 `updated_at` 而不是 `id`？**

| Bookmark Key | 捕获 INSERT | 捕获 UPDATE | 推荐 |
|-------------|------------|------------|------|
| `id` | ✅ | ❌ 漏掉更新 | 不推荐 |
| `updated_at` | ✅ | ✅ | **推荐** |

**MySQL 表必须满足的条件：**

```sql
-- updated_at 必须有 ON UPDATE CURRENT_TIMESTAMP, 否则更新不会被捕获
CREATE TABLE user_data (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    ...
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_updated_at (updated_at)  -- 索引加速增量查询
);
```

**Bookmark 生效的 3 个必要条件：**

1. Job 参数: `--job-bookmark-option` = `job-bookmark-enable`
2. 代码中: `transformation_ctx="datasource"` 必须设置（这是 bookmark 的标识符）
3. 代码末尾: `job.commit()` 必须调用（保存 bookmark 状态）

**容错机制：**

- Job 失败 → bookmark 不更新 → 下次重新读取这批数据
- 重复数据 → MERGE INTO 的 `ON t.id = s.id` 保证幂等，不会产生重复
- 即使同一批数据被处理两次，结果也是正确的

**手动重置 Bookmark（重新全量）：**

```bash
aws glue reset-job-bookmark --job-name etl-mysql-to-iceberg
```

## Step 10: Redshift - Query Iceberg via Spectrum

```bash
# Create external schema pointing to Glue Catalog
aws redshift-data execute-statement \
  --cluster-identifier etl-redshift --database etl_dw --db-user admin \
  --sql "CREATE EXTERNAL SCHEMA IF NOT EXISTS iceberg_schema
         FROM DATA CATALOG DATABASE 'etl_catalog_db'
         IAM_ROLE 'arn:aws:iam::ACCOUNT:role/etl-redshift-s3-role';"
```

### Dimension View (pure SQL, no UDF needed)

```sql
-- Redshift natively supports || and CONCAT(), SPLIT_PART(), DATEDIFF()
CREATE OR REPLACE VIEW public.v_user_dimension AS
SELECT
    id,
    username,
    email,
    username || ' <' || email || '>' AS display_name,
    SPLIT_PART(email, '@', 2) AS email_domain,
    DATE_TRUNC('month', created_at) AS created_month,
    EXTRACT(YEAR FROM created_at) AS created_year,
    DATEDIFF(day, created_at, GETDATE()) AS tenure_days,
    CASE
        WHEN DATEDIFF(day, created_at, GETDATE()) < 30 THEN 'New'
        WHEN DATEDIFF(day, created_at, GETDATE()) < 365 THEN 'Active'
        ELSE 'Veteran'
    END AS user_segment,
    SPLIT_PART(address, ',', 1) AS address_line1,
    SPLIT_PART(address, ',', 2) AS city,
    phone, address, updated_at
FROM iceberg_schema.user_data_iceberg;
```

### Query Examples

```sql
-- Direct query on Iceberg table (no COPY needed)
SELECT * FROM iceberg_schema.user_data_iceberg ORDER BY id;

-- Dimension analysis
SELECT * FROM public.v_user_dimension;

-- Aggregation
SELECT user_segment, COUNT(*) FROM public.v_user_dimension GROUP BY 1;
```

## Step 11: Automated Scheduling

```bash
# Create Workflow
aws glue create-workflow --name etl-iceberg-workflow \
  --description "MySQL -> Iceberg (incremental + dedup)"

# Scheduled trigger (every hour)
aws glue create-trigger --name etl-hourly-trigger \
  --type SCHEDULED --schedule "cron(0 * * * ? *)" \
  --workflow-name etl-iceberg-workflow \
  --actions '[{"CrawlerName":"etl-mysql-crawler"}]'

# After crawler -> run ETL job
aws glue create-trigger --name etl-after-crawler \
  --type CONDITIONAL --workflow-name etl-iceberg-workflow \
  --predicate '{"Conditions":[{"CrawlerName":"etl-mysql-crawler","LogicalOperator":"EQUALS","CrawlState":"SUCCEEDED"}]}' \
  --actions '[{"JobName":"etl-mysql-to-iceberg"}]'

# Activate triggers
aws glue start-trigger --name etl-hourly-trigger
aws glue start-trigger --name etl-after-crawler
```

## Step 12: Test Dedup

```sql
-- Insert duplicates + updates in MySQL
INSERT INTO user_data (id, username, email, phone, address)
VALUES (1, 'john_updated', 'john_new@example.com', '555-9999', '999 New St, Chicago, IL')
ON DUPLICATE KEY UPDATE username=VALUES(username), email=VALUES(email),
                        phone=VALUES(phone), address=VALUES(address);

-- Run the job
-- aws glue start-job-run --job-name etl-mysql-to-iceberg

-- Verify in Redshift: id=1 should show updated data, total count unchanged
SELECT * FROM iceberg_schema.user_data_iceberg WHERE id = 1;
```

## Security Checklist

| Resource | Public Access | Network |
|----------|-------------|---------|
| RDS MySQL | ❌ `PubliclyAccessible=false` | Private subnet only |
| Redshift | ❌ `PubliclyAccessible=false` | Private subnet only |
| S3 Bucket | ❌ All public access blocked | VPC Gateway Endpoint |
| Security Group | ❌ No `0.0.0.0/0` rules | VPC CIDR only (13.14.0.0/16) |
| Glue Jobs | N/A | Runs in VPC via Connection |

## PII Masking

| Field | Method | Example |
|-------|--------|---------|
| username | SHA-256 first 16 chars | `f76de6fe84487696` |
| email | SHA-256 prefix + @masked.com | `13779dbe@masked.com` |
| phone, address | Not masked | Original values preserved |

## Troubleshooting

| Issue | Solution |
|-------|---------|
| `Lake Formation permission` | Grant ALL on database + table to Glue/Redshift roles |
| `SCHEMA_NOT_FOUND` | Use `glue_catalog.db.table` with `--conf` parameter |
| `warehousePath must not be null` | Set `spark.sql.catalog.glue_catalog.warehouse` in `--conf` |
| Crawler slow (3-5 min) | Normal for JDBC - Spark cold start + VPC ENI creation |
| Bookmark not working | Ensure `transformation_ctx` is set and `job.commit()` called |

## Cost Optimization

- Glue: Use `G.1X` workers (smallest), 2 workers minimum
- Redshift: `ra3.xlplus` single-node for dev/test
- RDS: `db.t3.micro` for dev/test
- S3: Iceberg auto-compaction reduces small files
- Schedule: Adjust cron frequency based on data freshness needs
