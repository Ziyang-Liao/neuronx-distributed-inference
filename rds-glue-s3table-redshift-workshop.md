# RDS → Glue → S3 Iceberg → Redshift ETL Workshop

> 完整的增量 ETL 管道：从 RDS MySQL 增量抽取数据，PII 脱敏，写入 S3 Iceberg 表（自动去重），Redshift 通过 Spectrum 直接查询。

## 架构图

```
RDS MySQL (内网)
    │
    ▼ [Glue 5.0 ETL Job]
    │  ① Job Bookmark 增量抽取 (按 updated_at, 只读新增/更新)
    │  ② PII 脱敏: username → SHA256, email → hash@masked.com
    │  ③ MERGE INTO Iceberg (按 id upsert, 自动去重)
    │
S3 Iceberg Table (Glue Catalog 管理, 文件自动维护)
    │
    ▼ [Redshift Spectrum 直读, 无需 COPY]
Redshift (内网)
    └── iceberg_schema.user_data_iceberg (外部表)
    └── v_user_dimension (维度计算视图, 纯 SQL)
```

## 安全要求

- 所有资源部署在私有子网，不可公网访问
- 安全组仅允许 VPC CIDR 内部通信，禁止 0.0.0.0/0
- S3 阻止所有公共访问，通过 VPC Gateway Endpoint 访问

---

## Step 1: 网络配置

### 1.1 创建私有子网（两个 AZ，RDS/Redshift 需要）

```bash
VPC_ID="<your-vpc-id>"

# AZ-a 私有子网
aws ec2 create-subnet \
  --vpc-id $VPC_ID \
  --cidr-block <subnet-cidr-1> \
  --availability-zone us-east-1a \
  --tag-specifications 'ResourceType=subnet,Tags=[{Key=Name,Value=etl-private-subnet-1a}]' \
  --region us-east-1

# AZ-b 私有子网
aws ec2 create-subnet \
  --vpc-id $VPC_ID \
  --cidr-block <subnet-cidr-2> \
  --availability-zone us-east-1b \
  --tag-specifications 'ResourceType=subnet,Tags=[{Key=Name,Value=etl-private-subnet-1b}]' \
  --region us-east-1
```

> 验证: `MapPublicIpOnLaunch` 必须为 `false`

### 1.2 关联到有 NAT Gateway 的路由表

Glue Job 运行在 VPC 内，需要 NAT Gateway 出站访问 Glue 服务端点。

```bash
ROUTE_TABLE_ID="<nat-route-table-id>"

aws ec2 associate-route-table --route-table-id $ROUTE_TABLE_ID --subnet-id <subnet-1a-id> --region us-east-1
aws ec2 associate-route-table --route-table-id $ROUTE_TABLE_ID --subnet-id <subnet-1b-id> --region us-east-1
```

> 验证: 路由表中应有 `0.0.0.0/0 → nat-xxx` 路由

### 1.3 创建安全组

```bash
SG_ID=$(aws ec2 create-security-group \
  --group-name etl-pipeline-sg \
  --description "ETL pipeline - VPC internal only" \
  --vpc-id $VPC_ID \
  --region us-east-1 \
  --query 'GroupId' --output text)

# MySQL 3306 - 仅 VPC CIDR
aws ec2 authorize-security-group-ingress --group-id $SG_ID \
  --protocol tcp --port 3306 --cidr <vpc-cidr> --region us-east-1

# Redshift 5439 - 仅 VPC CIDR
aws ec2 authorize-security-group-ingress --group-id $SG_ID \
  --protocol tcp --port 5439 --cidr <vpc-cidr> --region us-east-1

# HTTPS 443 - VPC Endpoint 通信
aws ec2 authorize-security-group-ingress --group-id $SG_ID \
  --protocol tcp --port 443 --cidr <vpc-cidr> --region us-east-1

# 自引用 - Glue 节点间通信（必须）
aws ec2 authorize-security-group-ingress --group-id $SG_ID \
  --protocol -1 --source-group $SG_ID --region us-east-1
```

> 验证: 入站规则应有 4 条，无任何 `0.0.0.0/0` 规则

### 1.4 创建 S3 VPC Gateway Endpoint

```bash
aws ec2 create-vpc-endpoint \
  --vpc-id $VPC_ID \
  --service-name com.amazonaws.us-east-1.s3 \
  --vpc-endpoint-type Gateway \
  --route-table-ids $ROUTE_TABLE_ID \
  --tag-specifications 'ResourceType=vpc-endpoint,Tags=[{Key=Name,Value=etl-s3-endpoint}]' \
  --region us-east-1
```

> 验证: `State` = `available`, 路由表中应出现 `pl-xxx → vpce-xxx` 路由

### 1.5 启用 VPC DNS

```bash
aws ec2 modify-vpc-attribute --vpc-id $VPC_ID --enable-dns-hostnames '{"Value":true}' --region us-east-1
```

---

## Step 2: RDS MySQL

### 2.1 创建 DB 子网组

```bash
aws rds create-db-subnet-group \
  --db-subnet-group-name etl-db-subnet-group \
  --db-subnet-group-description "ETL pipeline private subnets" \
  --subnet-ids <subnet-1a-id> <subnet-1b-id> \
  --region us-east-1
```

### 2.2 创建 MySQL 实例

```bash
aws rds create-db-instance \
  --db-instance-identifier etl-mysql \
  --db-instance-class db.t3.micro \
  --engine mysql --engine-version 8.0 \
  --master-username admin --master-user-password '<your-password>' \
  --db-name etl_source \
  --db-subnet-group-name etl-db-subnet-group \
  --vpc-security-group-ids $SG_ID \
  --allocated-storage 20 --storage-type gp3 \
  --no-publicly-accessible \
  --region us-east-1
```

> 验证: `PubliclyAccessible` = `false`, `DBInstanceStatus` = `available`

### 2.3 初始化 MySQL 表

由于 MySQL 在私有子网，使用 Glue Python Shell Job 初始化数据：

```python
# glue_init_mysql.py
import pymysql

conn = pymysql.connect(
    host='<rds-endpoint>', port=3306,
    user='admin', password='<your-password>', database='etl_source'
)
cur = conn.cursor()

# 注意: updated_at 必须有 ON UPDATE CURRENT_TIMESTAMP，这是增量抽取的关键
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
cur.executemany(
    "INSERT INTO user_data (username,email,phone,address) VALUES (%s,%s,%s,%s)", data
)
conn.commit()
cur.close()
conn.close()
```

```bash
# 上传并运行
aws s3 cp glue_init_mysql.py s3://<your-bucket>/scripts/

aws glue create-job --name etl-init-mysql-data \
  --role arn:aws:iam::<account-id>:role/etl-glue-role \
  --command '{"Name":"pythonshell","ScriptLocation":"s3://<your-bucket>/scripts/glue_init_mysql.py","PythonVersion":"3.9"}' \
  --connections '{"Connections":["etl-mysql-connection"]}' \
  --default-arguments '{"--additional-python-modules":"pymysql"}' \
  --glue-version 3.0 --max-capacity 0.0625 \
  --region us-east-1

aws glue start-job-run --job-name etl-init-mysql-data --region us-east-1
```

---

## Step 3: S3 Bucket

```bash
BUCKET="etl-pipeline-data-<account-id>"

aws s3api create-bucket --bucket $BUCKET --region us-east-1

# 阻止所有公共访问（必须）
aws s3api put-public-access-block --bucket $BUCKET \
  --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
```

> 验证: 4 项 Public Access Block 全部为 `true`

---

## Step 4: Redshift

### 4.1 创建子网组和集群

```bash
aws redshift create-cluster-subnet-group \
  --cluster-subnet-group-name etl-redshift-subnet-group \
  --description "ETL Redshift private subnets" \
  --subnet-ids <subnet-1a-id> <subnet-1b-id> \
  --region us-east-1

aws redshift create-cluster \
  --cluster-identifier etl-redshift \
  --node-type ra3.xlplus --cluster-type single-node \
  --master-username admin --master-user-password '<your-password>' \
  --db-name etl_dw \
  --cluster-subnet-group-name etl-redshift-subnet-group \
  --vpc-security-group-ids $SG_ID \
  --no-publicly-accessible \
  --region us-east-1
```

> 验证: `PubliclyAccessible` = `false`, `ClusterStatus` = `available`

---

## Step 5: IAM Roles

### 5.1 Glue Role

```bash
# 创建角色
aws iam create-role --role-name etl-glue-role \
  --assume-role-policy-document '{
    "Version":"2012-10-17",
    "Statement":[{
      "Effect":"Allow",
      "Principal":{"Service":"glue.amazonaws.com"},
      "Action":"sts:AssumeRole"
    }]
  }'

# 附加托管策略
aws iam attach-role-policy --role-name etl-glue-role \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSGlueServiceRole

# 内联策略: S3 + Glue Catalog + Lake Formation + CloudWatch
aws iam put-role-policy --role-name etl-glue-role \
  --policy-name etl-glue-policy \
  --policy-document '{
    "Version":"2012-10-17",
    "Statement":[
      {
        "Effect":"Allow",
        "Action":["s3:GetObject","s3:PutObject","s3:DeleteObject","s3:ListBucket","s3:GetBucketLocation"],
        "Resource":"*"
      },
      {
        "Effect":"Allow",
        "Action":["glue:*Database*","glue:*Table*","glue:*Partition*","glue:*Catalog*"],
        "Resource":"*"
      },
      {
        "Effect":"Allow",
        "Action":["lakeformation:GetDataAccess"],
        "Resource":"*"
      },
      {
        "Effect":"Allow",
        "Action":["logs:CreateLogGroup","logs:CreateLogStream","logs:PutLogEvents"],
        "Resource":"*"
      }
    ]
  }'
```

### 5.2 Redshift Role

```bash
aws iam create-role --role-name etl-redshift-s3-role \
  --assume-role-policy-document '{
    "Version":"2012-10-17",
    "Statement":[{
      "Effect":"Allow",
      "Principal":{"Service":"redshift.amazonaws.com"},
      "Action":"sts:AssumeRole"
    }]
  }'

aws iam put-role-policy --role-name etl-redshift-s3-role \
  --policy-name etl-redshift-s3-read \
  --policy-document '{
    "Version":"2012-10-17",
    "Statement":[
      {
        "Effect":"Allow",
        "Action":["s3:GetObject","s3:ListBucket","s3:GetBucketLocation"],
        "Resource":"*"
      },
      {
        "Effect":"Allow",
        "Action":["glue:*Database*","glue:*Table*","glue:*Partition*","glue:*Catalog*"],
        "Resource":"*"
      },
      {
        "Effect":"Allow",
        "Action":["lakeformation:GetDataAccess"],
        "Resource":"*"
      }
    ]
  }'

# 关联到 Redshift 集群（等集群 available 后执行）
aws redshift modify-cluster-iam-roles \
  --cluster-identifier etl-redshift \
  --add-iam-roles arn:aws:iam::<account-id>:role/etl-redshift-s3-role \
  --region us-east-1
```

> 验证: Redshift `IamRoles` 中 `ApplyStatus` = `in-sync`

---

## Step 6: Lake Formation 权限

```bash
ACCOUNT="<account-id>"

# Glue Role - 数据库级别
aws lakeformation grant-permissions \
  --principal "{\"DataLakePrincipalIdentifier\":\"arn:aws:iam::${ACCOUNT}:role/etl-glue-role\"}" \
  --resource "{\"Database\":{\"Name\":\"etl_catalog_db\"}}" \
  --permissions ALL \
  --region us-east-1

# Glue Role - 表级别
aws lakeformation grant-permissions \
  --principal "{\"DataLakePrincipalIdentifier\":\"arn:aws:iam::${ACCOUNT}:role/etl-glue-role\"}" \
  --resource "{\"Table\":{\"DatabaseName\":\"etl_catalog_db\",\"TableWildcard\":{}}}" \
  --permissions ALL \
  --region us-east-1

# Redshift Role - 数据库级别
aws lakeformation grant-permissions \
  --principal "{\"DataLakePrincipalIdentifier\":\"arn:aws:iam::${ACCOUNT}:role/etl-redshift-s3-role\"}" \
  --resource "{\"Database\":{\"Name\":\"etl_catalog_db\"}}" \
  --permissions ALL \
  --region us-east-1

# Redshift Role - 表级别
aws lakeformation grant-permissions \
  --principal "{\"DataLakePrincipalIdentifier\":\"arn:aws:iam::${ACCOUNT}:role/etl-redshift-s3-role\"}" \
  --resource "{\"Table\":{\"DatabaseName\":\"etl_catalog_db\",\"TableWildcard\":{}}}" \
  --permissions ALL \
  --region us-east-1
```

---

## Step 7: Glue Catalog + Connection + Crawler

### 7.1 创建 Glue Catalog 数据库

```bash
aws glue create-database \
  --database-input '{"Name":"etl_catalog_db","Description":"ETL pipeline catalog"}' \
  --region us-east-1
```

### 7.2 创建 MySQL JDBC 连接

```bash
aws glue create-connection --connection-input '{
  "Name": "etl-mysql-connection",
  "ConnectionType": "JDBC",
  "ConnectionProperties": {
    "JDBC_CONNECTION_URL": "jdbc:mysql://<rds-endpoint>:3306/etl_source",
    "USERNAME": "admin",
    "PASSWORD": "<your-password>"
  },
  "PhysicalConnectionRequirements": {
    "SubnetId": "<subnet-1a-id>",
    "SecurityGroupIdList": ["<sg-id>"],
    "AvailabilityZone": "us-east-1a"
  }
}' --region us-east-1
```

> 验证: Connection 的 SubnetId 和 SecurityGroupIdList 必须与 RDS 在同一 VPC

### 7.3 创建 Crawler

```bash
aws glue create-crawler \
  --name etl-mysql-crawler \
  --role arn:aws:iam::<account-id>:role/etl-glue-role \
  --database-name etl_catalog_db \
  --targets '{"JdbcTargets":[{"ConnectionName":"etl-mysql-connection","Path":"etl_source/%"}]}' \
  --region us-east-1
```

### 7.4 运行 Crawler

```bash
aws glue start-crawler --name etl-mysql-crawler --region us-east-1

# 等待完成（约 3-5 分钟，JDBC Crawler 有冷启动开销）
aws glue get-crawler --name etl-mysql-crawler --region us-east-1 \
  --query 'Crawler.{State:State,LastCrawl:LastCrawl.Status}'
```

> 验证: Catalog 中应出现表 `etl_source_user_data`，包含 7 列: id, username, email, phone, address, created_at, updated_at

---

## Step 8: Glue 5.0 ETL Job（核心）

这是整个管道的核心，一个 Job 完成：增量抽取 → 脱敏 → MERGE INTO Iceberg（自动去重）。

### 8.1 Job 脚本: `glue_mysql_to_iceberg.py`

```python
"""
Glue 5.0 ETL: MySQL -> Iceberg with incremental MERGE + PII masking.
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

# 1. 增量抽取: bookmark 按 updated_at 排序，只读上次之后的新增/更新记录
datasource = glueContext.create_dynamic_frame.from_catalog(
    database="etl_catalog_db",
    table_name="etl_source_user_data",
    transformation_ctx="datasource",       # 必须设置，bookmark 的标识符
    additional_options={
        "jobBookmarkKeys": ["updated_at"],  # 按 updated_at 做增量（捕获 INSERT + UPDATE）
        "jobBookmarkKeysSortOrder": "asc"
    }
)

df = datasource.toDF()
if df.count() == 0:
    print("No new/updated records.")
    job.commit()                            # 必须调用，保存 bookmark 状态
    sys.exit(0)

record_count = df.count()
print(f"Extracted {record_count} incremental records")

# 2. PII 脱敏
for col_name in ["username", "email"]:
    if col_name in df.columns:
        if "email" in col_name:
            df = df.withColumn(col_name,
                concat(sha2(col(col_name), 256).substr(1, 8), lit("@masked.com")))
        else:
            df = df.withColumn(col_name,
                sha2(col(col_name), 256).substr(1, 16))

# 3. 创建 Iceberg 表（首次运行时创建，后续跳过）
spark.sql("""
    CREATE TABLE IF NOT EXISTS glue_catalog.etl_catalog_db.user_data_iceberg (
        id BIGINT, username STRING, email STRING, phone STRING,
        address STRING, created_at TIMESTAMP, updated_at TIMESTAMP
    ) USING iceberg
    LOCATION 's3://<your-bucket>/iceberg/user_data/'
    TBLPROPERTIES ('format-version'='2')
""")

# 4. MERGE INTO: 增量 upsert + 自动去重
#    - id 匹配且 updated_at 更新 → UPDATE（覆盖更新）
#    - id 不存在 → INSERT（新增）
#    - id 匹配但 updated_at 更旧 → 不操作（防止旧数据回写）
df.createOrReplaceTempView("incremental_data")
spark.sql("""
    MERGE INTO glue_catalog.etl_catalog_db.user_data_iceberg t
    USING incremental_data s ON t.id = s.id
    WHEN MATCHED AND s.updated_at > t.updated_at THEN UPDATE SET *
    WHEN NOT MATCHED THEN INSERT *
""")

total = spark.sql(
    "SELECT COUNT(*) FROM glue_catalog.etl_catalog_db.user_data_iceberg"
).collect()[0][0]
print(f"MERGE complete. Incremental: {record_count}, Total in Iceberg: {total}")

job.commit()  # 必须调用，保存 bookmark
```

### 8.2 上传脚本

```bash
aws s3 cp glue_mysql_to_iceberg.py s3://<your-bucket>/scripts/ --region us-east-1
```

### 8.3 创建 Glue Job

```bash
aws glue create-job \
  --name etl-mysql-to-iceberg \
  --role arn:aws:iam::<account-id>:role/etl-glue-role \
  --glue-version 5.0 \
  --worker-type G.1X \
  --number-of-workers 2 \
  --connections '{"Connections":["etl-mysql-connection"]}' \
  --command '{
    "Name": "glueetl",
    "ScriptLocation": "s3://<your-bucket>/scripts/glue_mysql_to_iceberg.py",
    "PythonVersion": "3"
  }' \
  --default-arguments '{
    "--job-bookmark-option": "job-bookmark-enable",
    "--datalake-formats": "iceberg",
    "--conf": "spark.sql.extensions=org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions --conf spark.sql.catalog.glue_catalog=org.apache.iceberg.spark.SparkCatalog --conf spark.sql.catalog.glue_catalog.warehouse=s3://<your-bucket>/iceberg/ --conf spark.sql.catalog.glue_catalog.catalog-impl=org.apache.iceberg.aws.glue.GlueCatalog --conf spark.sql.catalog.glue_catalog.io-impl=org.apache.iceberg.aws.s3.S3FileIO"
  }' \
  --region us-east-1
```

### 8.4 `--conf` 参数说明

| 配置项 | 作用 |
|--------|------|
| `spark.sql.extensions=...IcebergSparkSessionExtensions` | 启用 Iceberg SQL 扩展（MERGE INTO 等） |
| `spark.sql.catalog.glue_catalog=...SparkCatalog` | 注册名为 `glue_catalog` 的 Spark Catalog |
| `spark.sql.catalog.glue_catalog.warehouse=s3://...` | Iceberg 数据文件存储位置 |
| `spark.sql.catalog.glue_catalog.catalog-impl=...GlueCatalog` | 使用 Glue Data Catalog 管理元数据 |
| `spark.sql.catalog.glue_catalog.io-impl=...S3FileIO` | 使用 S3 作为文件 IO |

### 8.5 首次运行

```bash
aws glue start-job-run --job-name etl-mysql-to-iceberg --region us-east-1
```

> 验证: 日志应显示 `Extracted N incremental records` 和 `MERGE complete`
> Glue Catalog 中应出现 `user_data_iceberg` 表，类型为 `ICEBERG`

---

## Step 9: 增量抽取原理（重要）

### 工作机制

Glue Job Bookmark 记录每次运行读到的 `updated_at` 最大值，下次只读大于该值的记录：

```
第1次运行: SELECT * FROM user_data
           → 读取全部 12 条
           bookmark 记录 max(updated_at) = 2026-03-23 16:08:00

第2次运行: SELECT * FROM user_data WHERE updated_at > '2026-03-23 16:08:00'
           → 只读取 5 条 (2 条 UPDATE + 3 条 INSERT)
           bookmark 更新 max(updated_at) = 2026-03-23 16:57:20

第3次运行: 如果没有新数据 → 读取 0 条, 直接退出
```

### 为什么用 `updated_at` 而不是 `id`

| Bookmark Key | 捕获 INSERT | 捕获 UPDATE | 推荐 |
|-------------|------------|------------|------|
| `id` | ✅ | ❌ 漏掉更新 | 不推荐 |
| `updated_at` | ✅ | ✅ | **推荐** |

### MySQL 表必须满足的条件

```sql
-- updated_at 必须有 ON UPDATE CURRENT_TIMESTAMP
updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
-- 建议加索引，加速增量查询
INDEX idx_updated_at (updated_at)
```

### Bookmark 生效的 3 个必要条件

1. Job 参数: `--job-bookmark-option` = `job-bookmark-enable`
2. 代码中: `transformation_ctx="datasource"` 必须设置
3. 代码末尾: `job.commit()` 必须调用

### 容错机制

- Job 失败 → bookmark 不更新 → 下次重新读取这批数据
- 重复数据 → MERGE INTO 的 `ON t.id = s.id` 保证幂等
- 手动重置: `aws glue reset-job-bookmark --job-name etl-mysql-to-iceberg`

---

## Step 10: Redshift 配置

### 10.1 创建外部 Schema（指向 Glue Catalog）

```bash
aws redshift-data execute-statement \
  --cluster-identifier etl-redshift \
  --database etl_dw --db-user admin \
  --sql "CREATE EXTERNAL SCHEMA IF NOT EXISTS iceberg_schema
         FROM DATA CATALOG DATABASE 'etl_catalog_db'
         IAM_ROLE 'arn:aws:iam::<account-id>:role/etl-redshift-s3-role';" \
  --region us-east-1
```

> 验证: `SELECT schemaname FROM svv_external_schemas WHERE schemaname='iceberg_schema';` 应返回 1 行

### 10.2 查询 Iceberg 表（无需 COPY，直接读）

```sql
SELECT * FROM iceberg_schema.user_data_iceberg ORDER BY id;
```

### 10.3 创建维度计算视图

Redshift 原生支持 `||`、`CONCAT()`、`SPLIT_PART()`、`DATEDIFF()` 等函数，**不需要 UDF**。

```sql
CREATE OR REPLACE VIEW public.v_user_dimension AS
SELECT
    id,
    username,
    email,
    -- 字段拼接: 原生 || 操作符
    username || ' <' || email || '>' AS display_name,
    -- 字段解析
    SPLIT_PART(email, '@', 2) AS email_domain,
    -- 时间维度
    DATE_TRUNC('month', created_at) AS created_month,
    EXTRACT(YEAR FROM created_at) AS created_year,
    -- 计算维度
    DATEDIFF(day, created_at, GETDATE()) AS tenure_days,
    CASE
        WHEN DATEDIFF(day, created_at, GETDATE()) < 30 THEN 'New'
        WHEN DATEDIFF(day, created_at, GETDATE()) < 365 THEN 'Active'
        ELSE 'Veteran'
    END AS user_segment,
    -- 地址解析
    SPLIT_PART(address, ',', 1) AS address_line1,
    SPLIT_PART(address, ',', 2) AS city,
    phone, address, updated_at
FROM iceberg_schema.user_data_iceberg;
```

### 10.4 查询示例

```sql
-- 维度分析
SELECT * FROM public.v_user_dimension;

-- 用户分群统计
SELECT user_segment, COUNT(*) AS cnt FROM public.v_user_dimension GROUP BY 1;

-- 月度新增趋势
SELECT created_month, COUNT(*) FROM public.v_user_dimension GROUP BY 1 ORDER BY 1;
```

---

## Step 11: 定时调度

### 11.1 创建 Workflow

```bash
aws glue create-workflow \
  --name etl-iceberg-workflow \
  --description "MySQL -> Iceberg incremental ETL" \
  --region us-east-1
```

### 11.2 创建触发器

```bash
# 定时触发 Crawler（每小时，可按需调整）
aws glue create-trigger \
  --name etl-scheduled-trigger \
  --type SCHEDULED \
  --schedule "cron(0 * * * ? *)" \
  --workflow-name etl-iceberg-workflow \
  --actions '[{"CrawlerName":"etl-mysql-crawler"}]' \
  --region us-east-1

# Crawler 成功后触发 ETL Job
aws glue create-trigger \
  --name etl-after-crawler-trigger \
  --type CONDITIONAL \
  --workflow-name etl-iceberg-workflow \
  --predicate '{"Conditions":[{"CrawlerName":"etl-mysql-crawler","LogicalOperator":"EQUALS","CrawlState":"SUCCEEDED"}]}' \
  --actions '[{"JobName":"etl-mysql-to-iceberg"}]' \
  --region us-east-1
```

### 11.3 激活触发器

```bash
aws glue start-trigger --name etl-scheduled-trigger --region us-east-1
aws glue start-trigger --name etl-after-crawler-trigger --region us-east-1
```

> 验证: `aws glue get-trigger --name etl-scheduled-trigger --query 'Trigger.State'` 应为 `ACTIVATED`

### 11.4 自动化流程

```
cron(0 * * * ? *)  每小时触发
    │
    ▼
Crawler [etl-mysql-crawler]         ← 更新 MySQL 元数据到 Glue Catalog
    │ SUCCEEDED
    ▼
Job [etl-mysql-to-iceberg]          ← 增量抽取 + 脱敏 + MERGE INTO Iceberg
    │
    ▼
Redshift 直接查询 iceberg_schema    ← 数据实时可见，无需额外操作
```

---

## Step 12: 验证去重效果

### 12.1 插入更新 + 新增数据

```sql
-- 在 MySQL 中执行（通过 Glue Python Shell Job）
-- UPDATE: 修改 id=1 的地址和电话
UPDATE user_data SET address='100 Lake Shore Dr, Chicago, IL', phone='555-0000' WHERE id=1;

-- INSERT: 新增 3 条记录
INSERT INTO user_data (username, email, phone, address) VALUES
('henry_ford', 'henry@example.com', '555-1300', '500 Motor Ave, Detroit, MI'),
('iris_zhang', 'iris@example.com', '555-1400', '600 Tech Blvd, San Francisco, CA'),
('jack_ryan', 'jack@example.com', '555-1500', '700 Intel St, Langley, VA');
```

### 12.2 运行 ETL Job

```bash
aws glue start-job-run --job-name etl-mysql-to-iceberg --region us-east-1
```

### 12.3 验证结果

日志应显示:
```
Extracted 5 incremental records          ← 不是全量 15 条，只读了 5 条增量
MERGE complete. Incremental: 5, Total in Iceberg: 15
```

在 Redshift 中验证:
```sql
-- id=1 应该显示更新后的 Chicago 地址和 555-0000 电话
SELECT id, phone, address FROM iceberg_schema.user_data_iceberg WHERE id IN (1, 13, 14, 15);
```

| id | 操作 | 验证点 |
|----|------|--------|
| 1 | UPDATE | address 变为 Chicago, phone 变为 555-0000 |
| 13 | INSERT | 新增 henry_ford |
| 14 | INSERT | 新增 iris_zhang |
| 15 | INSERT | 新增 jack_ryan |
| 总数 | - | 15 条（不是 17 条，因为 MERGE 去重了 id=1,5 的更新） |

---

## PII 脱敏说明

| 字段 | 脱敏方法 | 示例 |
|------|---------|------|
| username | SHA-256 取前 16 字符 | `john_doe` → `f76de6fe84487696` |
| email | SHA-256 前 8 字符 + @masked.com | `john@example.com` → `13779dbe@masked.com` |
| phone | 不脱敏 | 保持原值 |
| address | 不脱敏 | 保持原值 |

---

## 安全检查清单

| 资源 | 公网访问 | 网络 | 验证命令 |
|------|---------|------|---------|
| RDS MySQL | ❌ | 私有子网 | `aws rds describe-db-instances --query '..PubliclyAccessible'` |
| Redshift | ❌ | 私有子网 | `aws redshift describe-clusters --query '..PubliclyAccessible'` |
| S3 Bucket | ❌ 全部阻止 | VPC Endpoint | `aws s3api get-public-access-block` |
| 安全组 | ❌ 无 0.0.0.0/0 | VPC CIDR only | `aws ec2 describe-security-groups --query '..IpRanges'` |
| Glue Job | N/A | VPC Connection | Connection 配置中指定 SubnetId + SG |

---

## 故障排查

| 问题 | 原因 | 解决 |
|------|------|------|
| `Lake Formation permission` | Glue/Redshift Role 缺少 LF 权限 | Step 6 授权 |
| `SCHEMA_NOT_FOUND` | Spark 找不到 Glue Catalog | 确认 `--conf` 中 `glue_catalog` 配置正确 |
| `warehousePath must not be null` | 缺少 warehouse 配置 | `--conf` 中加 `spark.sql.catalog.glue_catalog.warehouse` |
| Crawler 慢 (3-5 min) | JDBC Crawler 冷启动 + VPC ENI 创建 | 正常现象 |
| Bookmark 不生效 | 缺少 transformation_ctx 或 job.commit() | 检查 Step 9 的 3 个必要条件 |
| `No new/updated records` | Bookmark 已记录到最新 | 确认 MySQL 有新数据，或 reset-job-bookmark |

---

## 成本优化建议

| 资源 | 开发/测试 | 生产 |
|------|----------|------|
| Glue Job | G.1X × 2 workers | 按数据量调整 workers |
| Redshift | ra3.xlplus single-node | ra3.xlplus multi-node |
| RDS | db.t3.micro | db.r6g 系列 |
| 调度频率 | 手动触发 | 按业务需求 (每小时/每天) |
| Iceberg | 默认配置 | 开启 auto-compaction 减少小文件 |
