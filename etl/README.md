# RDS → Glue → S3 Iceberg → Redshift ETL Workshop

> 完整的增量 ETL 管道：RDS MySQL 增量抽取 → PII 脱敏 → S3 Iceberg 表（MERGE 去重）→ Redshift 本地表（SP 增量同步）→ 维度计算视图

## 架构

```
RDS MySQL (内网)
    │
    ▼ [Glue 5.0 Job: glue_mysql_to_iceberg.py]
    │  ① Job Bookmark 增量抽取 (按 updated_at)
    │  ② PII 脱敏: username → SHA256, email → hash@masked.com
    │  ③ MERGE INTO Iceberg (按 id upsert, 自动去重)
    │
S3 Iceberg Table (Glue Catalog 管理)
    │
    ▼ [Redshift SP: CALL sp_sync_from_iceberg()]
    │  MERGE INTO iceberg_local (增量拉取, 幂等)
    │
Redshift 本地表 iceberg_local
    ├── mv_user_base (物化视图, AUTO REFRESH, 字段拼接/地址解析)
    ├── mv_city_stats (物化视图, AUTO REFRESH, 城市聚合)
    ├── mv_monthly_stats (物化视图, AUTO REFRESH, 日期聚合)
    └── v_user_dimension (普通视图, 含 GETDATE() 动态计算)
```

## 文件结构

```
etl/
├── README.md                    # 本文档 (完整操作指南)
├── glue_mysql_to_iceberg.py     # 核心 ETL Job (Step 8)
├── glue_init_mysql.py           # MySQL 初始化数据 (Step 2)
├── glue_add_duplicates.py       # 测试: 插入重复/更新数据 (Step 12)
├── glue_incremental_test.py     # 测试: 验证增量效果 (Step 12)
└── verify_workshop.sh           # 端到端验证脚本 (每步做完可运行检查)
```

## 安全要求

- 所有资源部署在私有子网，禁止公网访问
- 安全组仅允许 VPC CIDR 内部通信，禁止 0.0.0.0/0
- S3 阻止所有公共访问，通过 VPC Gateway Endpoint 访问

## 推荐执行顺序

部分步骤有依赖关系，推荐按以下顺序执行：

```
Step 1  网络 (VPC/子网/SG/Endpoint)
Step 2.1 RDS MySQL 创建（等待 available）
Step 3  S3 Bucket
Step 4  Redshift 创建（等待 available）
Step 5  IAM Roles
Step 7.1 Glue Catalog Database        ← Step 6 依赖此步
Step 6  Lake Formation 权限
Step 7.2 Glue Connection               ← 依赖 Step 1 (子网/SG) + Step 2 (RDS endpoint)
Step 7.3 Glue Crawler + 运行
Step 2.2 MySQL 初始化数据              ← 依赖 Step 7.2 (Connection)
Step 8  Glue ETL Job 创建 + 运行
Step 9  (阅读) 增量原理
Step 10 Redshift 配置 (Schema/表/SP/MV/View)
Step 11 定时调度
Step 12 验证
```

---

## Step 1: 网络配置

### 1.1 创建私有子网（两个 AZ）

```bash
VPC_ID="<your-vpc-id>"

aws ec2 create-subnet --vpc-id $VPC_ID --cidr-block <cidr-1> \
  --availability-zone us-east-1a \
  --tag-specifications 'ResourceType=subnet,Tags=[{Key=Name,Value=etl-private-subnet-1a}]' \
  --region us-east-1

aws ec2 create-subnet --vpc-id $VPC_ID --cidr-block <cidr-2> \
  --availability-zone us-east-1b \
  --tag-specifications 'ResourceType=subnet,Tags=[{Key=Name,Value=etl-private-subnet-1b}]' \
  --region us-east-1
```

> ✅ 验证: `MapPublicIpOnLaunch` = `false`

### 1.2 关联 NAT Gateway 路由表

```bash
aws ec2 associate-route-table --route-table-id <nat-rtb-id> --subnet-id <subnet-1a> --region us-east-1
aws ec2 associate-route-table --route-table-id <nat-rtb-id> --subnet-id <subnet-1b> --region us-east-1
```

> ✅ 验证: 路由表有 `0.0.0.0/0 → nat-xxx`

### 1.3 创建安全组

```bash
SG_ID=$(aws ec2 create-security-group --group-name etl-pipeline-sg \
  --description "ETL pipeline - VPC internal only" --vpc-id $VPC_ID \
  --region us-east-1 --query 'GroupId' --output text)

aws ec2 authorize-security-group-ingress --group-id $SG_ID --protocol tcp --port 3306 --cidr <vpc-cidr> --region us-east-1
aws ec2 authorize-security-group-ingress --group-id $SG_ID --protocol tcp --port 5439 --cidr <vpc-cidr> --region us-east-1
aws ec2 authorize-security-group-ingress --group-id $SG_ID --protocol tcp --port 443  --cidr <vpc-cidr> --region us-east-1
aws ec2 authorize-security-group-ingress --group-id $SG_ID --protocol -1 --source-group $SG_ID --region us-east-1
```

> ✅ 验证: 4 条入站规则，无 `0.0.0.0/0`

### 1.4 S3 VPC Gateway Endpoint

```bash
aws ec2 create-vpc-endpoint --vpc-id $VPC_ID \
  --service-name com.amazonaws.us-east-1.s3 --vpc-endpoint-type Gateway \
  --route-table-ids <nat-rtb-id> --region us-east-1
```

### 1.5 启用 VPC DNS

```bash
aws ec2 modify-vpc-attribute --vpc-id $VPC_ID --enable-dns-hostnames '{"Value":true}' --region us-east-1
```

---

## Step 2: RDS MySQL

### 2.1 创建实例

```bash
aws rds create-db-subnet-group --db-subnet-group-name etl-db-subnet-group \
  --db-subnet-group-description "ETL private subnets" \
  --subnet-ids <subnet-1a> <subnet-1b> --region us-east-1

aws rds create-db-instance \
  --db-instance-identifier etl-mysql \
  --db-instance-class db.t3.micro --engine mysql --engine-version 8.0 \
  --master-username admin --master-user-password '<your-password>' \
  --db-name etl_source \
  --db-subnet-group-name etl-db-subnet-group \
  --vpc-security-group-ids $SG_ID \
  --allocated-storage 20 --storage-type gp3 \
  --no-publicly-accessible --region us-east-1
```

> ✅ 验证: `PubliclyAccessible` = `false`

### 2.2 初始化数据

> ⚠️ 此步骤依赖 Step 7 的 Glue Connection，请先完成 Step 3-7 后再回来执行此步。

MySQL 在私有子网，通过 Glue Python Shell Job 初始化。脚本: [`glue_init_mysql.py`](glue_init_mysql.py)

**关键: MySQL 表必须有 `updated_at TIMESTAMP ON UPDATE CURRENT_TIMESTAMP` + 索引**，这是增量抽取的基础。

```bash
# 获取 RDS Endpoint（Step 2.1 创建完成后）
aws rds describe-db-instances --db-instance-identifier etl-mysql \
  --query 'DBInstances[0].Endpoint.Address' --output text --region us-east-1

# 修改 glue_init_mysql.py 中的 host 为上面获取的 endpoint
# 然后上传并运行
aws s3 cp glue_init_mysql.py s3://<your-bucket>/scripts/
aws glue create-job --name etl-init-mysql-data \
  --role arn:aws:iam::<account-id>:role/etl-glue-role \
  --command '{"Name":"pythonshell","ScriptLocation":"s3://<your-bucket>/scripts/glue_init_mysql.py","PythonVersion":"3.9"}' \
  --connections '{"Connections":["etl-mysql-connection"]}' \
  --default-arguments '{"--additional-python-modules":"pymysql"}' \
  --glue-version 3.0 --max-capacity 0.0625 --region us-east-1

aws glue start-job-run --job-name etl-init-mysql-data --region us-east-1
```

---

## Step 3: S3 Bucket

```bash
BUCKET="etl-pipeline-data-<account-id>"
aws s3api create-bucket --bucket $BUCKET --region us-east-1
aws s3api put-public-access-block --bucket $BUCKET \
  --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
```

> ✅ 验证: 4 项 Public Access Block 全部 `true`

---

## Step 4: Redshift

```bash
aws redshift create-cluster-subnet-group --cluster-subnet-group-name etl-redshift-subnet-group \
  --description "ETL Redshift private subnets" --subnet-ids <subnet-1a> <subnet-1b> --region us-east-1

aws redshift create-cluster \
  --cluster-identifier etl-redshift --node-type ra3.xlplus --cluster-type single-node \
  --master-username admin --master-user-password '<your-password>' \
  --db-name etl_dw \
  --cluster-subnet-group-name etl-redshift-subnet-group \
  --vpc-security-group-ids $SG_ID \
  --no-publicly-accessible --region us-east-1
```

> ✅ 验证: `PubliclyAccessible` = `false`

---

## Step 5: IAM Roles

### 5.1 Glue Role

```bash
aws iam create-role --role-name etl-glue-role \
  --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"glue.amazonaws.com"},"Action":"sts:AssumeRole"}]}'

aws iam attach-role-policy --role-name etl-glue-role \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSGlueServiceRole

aws iam put-role-policy --role-name etl-glue-role --policy-name etl-glue-policy \
  --policy-document '{
    "Version":"2012-10-17",
    "Statement":[
      {"Effect":"Allow","Action":["s3:GetObject","s3:PutObject","s3:DeleteObject","s3:ListBucket","s3:GetBucketLocation"],"Resource":"*"},
      {"Effect":"Allow","Action":["glue:*Database*","glue:*Table*","glue:*Partition*","glue:*Catalog*"],"Resource":"*"},
      {"Effect":"Allow","Action":["lakeformation:GetDataAccess"],"Resource":"*"},
      {"Effect":"Allow","Action":["logs:CreateLogGroup","logs:CreateLogStream","logs:PutLogEvents"],"Resource":"*"}
    ]}'
```

### 5.2 Redshift Role

```bash
aws iam create-role --role-name etl-redshift-s3-role \
  --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"redshift.amazonaws.com"},"Action":"sts:AssumeRole"}]}'

aws iam put-role-policy --role-name etl-redshift-s3-role --policy-name etl-redshift-s3-read \
  --policy-document '{
    "Version":"2012-10-17",
    "Statement":[
      {"Effect":"Allow","Action":["s3:GetObject","s3:ListBucket","s3:GetBucketLocation"],"Resource":"*"},
      {"Effect":"Allow","Action":["glue:*Database*","glue:*Table*","glue:*Partition*","glue:*Catalog*"],"Resource":"*"},
      {"Effect":"Allow","Action":["lakeformation:GetDataAccess"],"Resource":"*"}
    ]}'

# 关联到 Redshift（等集群 available 后）
aws redshift modify-cluster-iam-roles --cluster-identifier etl-redshift \
  --add-iam-roles arn:aws:iam::<account-id>:role/etl-redshift-s3-role --region us-east-1
```

> ✅ 验证: Redshift IamRoles ApplyStatus = `in-sync`

---

## Step 6: Lake Formation 权限

> ⚠️ 此步骤依赖 Step 7.1 的 Glue Database，请先执行 Step 7.1 创建 `etl_catalog_db`，再回来执行此步。

```bash
ACCOUNT="<account-id>"

# Glue Role
aws lakeformation grant-permissions --principal "{\"DataLakePrincipalIdentifier\":\"arn:aws:iam::${ACCOUNT}:role/etl-glue-role\"}" \
  --resource '{"Database":{"Name":"etl_catalog_db"}}' --permissions ALL --region us-east-1
aws lakeformation grant-permissions --principal "{\"DataLakePrincipalIdentifier\":\"arn:aws:iam::${ACCOUNT}:role/etl-glue-role\"}" \
  --resource '{"Table":{"DatabaseName":"etl_catalog_db","TableWildcard":{}}}' --permissions ALL --region us-east-1

# Redshift Role
aws lakeformation grant-permissions --principal "{\"DataLakePrincipalIdentifier\":\"arn:aws:iam::${ACCOUNT}:role/etl-redshift-s3-role\"}" \
  --resource '{"Database":{"Name":"etl_catalog_db"}}' --permissions ALL --region us-east-1
aws lakeformation grant-permissions --principal "{\"DataLakePrincipalIdentifier\":\"arn:aws:iam::${ACCOUNT}:role/etl-redshift-s3-role\"}" \
  --resource '{"Table":{"DatabaseName":"etl_catalog_db","TableWildcard":{}}}' --permissions ALL --region us-east-1
```

---

## Step 7: Glue Catalog + Connection + Crawler

```bash
# Catalog 数据库
aws glue create-database --database-input '{"Name":"etl_catalog_db"}' --region us-east-1

# MySQL JDBC 连接
aws glue create-connection --connection-input '{
  "Name":"etl-mysql-connection","ConnectionType":"JDBC",
  "ConnectionProperties":{"JDBC_CONNECTION_URL":"jdbc:mysql://<rds-endpoint>:3306/etl_source","USERNAME":"admin","PASSWORD":"<your-password>"},
  "PhysicalConnectionRequirements":{"SubnetId":"<subnet-1a>","SecurityGroupIdList":["<sg-id>"],"AvailabilityZone":"us-east-1a"}
}' --region us-east-1

# Crawler
aws glue create-crawler --name etl-mysql-crawler \
  --role arn:aws:iam::<account-id>:role/etl-glue-role \
  --database-name etl_catalog_db \
  --targets '{"JdbcTargets":[{"ConnectionName":"etl-mysql-connection","Path":"etl_source/%"}]}' \
  --region us-east-1

# 运行 Crawler（约 3-5 分钟）
aws glue start-crawler --name etl-mysql-crawler --region us-east-1
```

> ✅ 验证: Catalog 中出现表 `etl_source_user_data`，7 列

---

## Step 8: Glue 5.0 ETL Job（核心）

脚本: [`glue_mysql_to_iceberg.py`](glue_mysql_to_iceberg.py) — 一个 Job 完成全部工作。

### 8.1 创建 Job

```bash
aws s3 cp glue_mysql_to_iceberg.py s3://<your-bucket>/scripts/

aws glue create-job --name etl-mysql-to-iceberg \
  --role arn:aws:iam::<account-id>:role/etl-glue-role \
  --glue-version 5.0 --worker-type G.1X --number-of-workers 2 \
  --connections '{"Connections":["etl-mysql-connection"]}' \
  --command '{"Name":"glueetl","ScriptLocation":"s3://<your-bucket>/scripts/glue_mysql_to_iceberg.py","PythonVersion":"3"}' \
  --default-arguments '{
    "--job-bookmark-option":"job-bookmark-enable",
    "--datalake-formats":"iceberg",
    "--conf":"spark.sql.extensions=org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions --conf spark.sql.catalog.glue_catalog=org.apache.iceberg.spark.SparkCatalog --conf spark.sql.catalog.glue_catalog.warehouse=s3://<your-bucket>/iceberg/ --conf spark.sql.catalog.glue_catalog.catalog-impl=org.apache.iceberg.aws.glue.GlueCatalog --conf spark.sql.catalog.glue_catalog.io-impl=org.apache.iceberg.aws.s3.S3FileIO"
  }' --region us-east-1
```

### 8.2 `--conf` 参数说明

| 配置 | 作用 |
|------|------|
| `IcebergSparkSessionExtensions` | 启用 MERGE INTO 等 Iceberg SQL |
| `glue_catalog=SparkCatalog` | 注册 `glue_catalog` 供 SQL 引用 |
| `warehouse=s3://...` | Iceberg 数据文件存储位置 |
| `catalog-impl=GlueCatalog` | 用 Glue Data Catalog 管理元数据 |
| `io-impl=S3FileIO` | S3 文件读写 |

### 8.3 运行

```bash
aws glue start-job-run --job-name etl-mysql-to-iceberg --region us-east-1
```

> ✅ 验证: 日志显示 `Extracted N incremental records` + `MERGE complete`
> ✅ 验证: Catalog 出现 `user_data_iceberg` 表，类型 `ICEBERG`

---

## Step 9: 增量抽取原理（重要）

### 9.1 机制

Glue Job Bookmark 记录 `updated_at` 最大值，下次只读大于该值的记录：

```
第1次: 读全部 12 条, bookmark = max(updated_at) = 16:08:00
第2次: 只读 updated_at > 16:08:00 → 5 条 (2 UPDATE + 3 INSERT)
第3次: 无新数据 → 0 条, 直接退出
```

### 9.2 为什么用 `updated_at` 不用 `id`

| Key | INSERT | UPDATE | 推荐 |
|-----|--------|--------|------|
| `id` | ✅ | ❌ 漏掉 | 不推荐 |
| `updated_at` | ✅ | ✅ | **推荐** |

### 9.3 MySQL 表要求

```sql
updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
INDEX idx_updated_at (updated_at)
```

### 9.4 Bookmark 生效 3 个必要条件

1. Job 参数: `--job-bookmark-option` = `job-bookmark-enable`
2. 代码: `transformation_ctx="datasource"` 必须设置
3. 代码末尾: `job.commit()` 必须调用

### 9.5 容错

- Job 失败 → bookmark 不更新 → 下次重读（MERGE 幂等，不会重复）
- 手动重置: `aws glue reset-job-bookmark --job-name etl-mysql-to-iceberg`

---

## Step 10: Redshift 配置

> Redshift 在私有子网，无法直连。以下 SQL 通过 **Redshift Data API** 执行：
> ```bash
> aws redshift-data execute-statement --cluster-identifier etl-redshift \
>   --database etl_dw --db-user admin --sql "<SQL>" --region us-east-1
> ```
> 或使用 **Redshift Query Editor V2**（AWS 控制台，自动通过内网连接）。

### 10.1 外部 Schema（读 Iceberg）

```sql
CREATE EXTERNAL SCHEMA IF NOT EXISTS iceberg_schema
FROM DATA CATALOG DATABASE 'etl_catalog_db'
IAM_ROLE 'arn:aws:iam::<account-id>:role/etl-redshift-s3-role';
```

### 10.2 本地表（存计算数据）

Redshift 物化视图不支持外部表，所以需要本地表。

```sql
CREATE TABLE public.iceberg_local (
    id BIGINT, username VARCHAR(256), email VARCHAR(256),
    phone VARCHAR(64), address VARCHAR(512),
    created_at TIMESTAMP, updated_at TIMESTAMP
) DISTSTYLE AUTO SORTKEY(id);
```

### 10.3 增量同步存储过程

从 Iceberg 外部表增量 MERGE 到本地表。水位线来自本地表的 `MAX(updated_at)`。

```sql
CREATE OR REPLACE PROCEDURE public.sp_sync_from_iceberg()
AS $$
DECLARE
    v_watermark TIMESTAMP;
BEGIN
    -- 水位线: 本地表最大 updated_at（首次为 1970-01-01 → 拉全量）
    SELECT COALESCE(MAX(updated_at), '1970-01-01'::timestamp)
    INTO v_watermark FROM public.iceberg_local;

    -- 增量 MERGE: 用 >= 防止同秒数据遗漏，MERGE 幂等不会重复
    MERGE INTO public.iceberg_local
    USING (SELECT * FROM iceberg_schema.user_data_iceberg WHERE updated_at >= v_watermark) src
    ON public.iceberg_local.id = src.id
    WHEN MATCHED THEN UPDATE SET
        username=src.username, email=src.email, phone=src.phone,
        address=src.address, created_at=src.created_at, updated_at=src.updated_at
    WHEN NOT MATCHED THEN INSERT
        VALUES (src.id, src.username, src.email, src.phone,
                src.address, src.created_at, src.updated_at);
END;
$$ LANGUAGE plpgsql;
```

**为什么用 `>=` 而不是 `>`：** 防止同一秒内多条记录只拉到部分。`>=` 会重复拉上次最后一秒的数据，但 MERGE ON id 保证幂等，不会产生重复行。

**为什么安全：**
- Iceberg 源数据已去重（Glue MERGE 保证）
- MERGE ON id：匹配则覆盖，不匹配则插入
- 可重复执行，结果一致

### 10.4 物化视图（AUTO REFRESH，预计算聚合）

物化视图预计算结果存储在本地，查询秒回。`AUTO REFRESH YES` 让 Redshift 在基表数据变化后自动刷新。

> 注意：含 `GETDATE()`/`DATEDIFF()` 等可变函数的不支持 AUTO REFRESH，需用普通视图。

```sql
-- 用户基础维度（字段拼接、地址解析）
CREATE MATERIALIZED VIEW public.mv_user_base AUTO REFRESH YES AS
SELECT id, username, email,
    username || ' <' || email || '>' AS display_name,
    SPLIT_PART(email, '@', 2) AS email_domain,
    SPLIT_PART(address, ',', 1) AS address_line1,
    TRIM(SPLIT_PART(address, ',', 2)) AS city,
    phone, address, created_at, updated_at
FROM public.iceberg_local;

-- 城市维度聚合
CREATE MATERIALIZED VIEW public.mv_city_stats AUTO REFRESH YES AS
SELECT TRIM(SPLIT_PART(address, ',', 2)) AS city,
    COUNT(*) AS user_count
FROM public.iceberg_local GROUP BY 1;

-- 日期维度聚合
CREATE MATERIALIZED VIEW public.mv_monthly_stats AUTO REFRESH YES AS
SELECT created_at::DATE AS created_date,
    COUNT(*) AS new_users
FROM public.iceberg_local GROUP BY 1;
```

### 10.5 普通视图（含动态计算）

含 `GETDATE()` 的计算必须用普通视图，每次查询实时计算。

```sql
CREATE OR REPLACE VIEW public.v_user_dimension AS
SELECT
    id, username, email,
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
FROM public.iceberg_local;
```

### 10.6 普通视图 vs 物化视图

| | 普通视图 `v_user_dimension` | 物化视图 `mv_*` |
|---|---|---|
| 数据 | 实时（每次查都算） | 预计算快照 |
| 速度 | 数据量大时慢 | 秒回 |
| 适合 | 含 `GETDATE()` 等动态函数 | 静态字段拼接、聚合统计 |
| 刷新 | 不需要 | AUTO REFRESH YES（自动） |
| 限制 | 无 | 不支持可变函数 |

### 10.7 执行同步 + 查询

```sql
-- 同步数据（物化视图会自动刷新，无需手动 REFRESH）
CALL public.sp_sync_from_iceberg();

-- 查询物化视图（秒回）
SELECT * FROM public.mv_user_base;
SELECT * FROM public.mv_city_stats ORDER BY user_count DESC;
SELECT * FROM public.mv_monthly_stats;

-- 查询普通视图（实时计算）
SELECT * FROM public.v_user_dimension;
SELECT user_segment, COUNT(*) FROM public.v_user_dimension GROUP BY 1;
```

---

## Step 11: 定时调度

### 11.1 Glue Workflow（MySQL → Iceberg 自动化）

```bash
aws glue create-workflow --name etl-iceberg-workflow \
  --description "MySQL -> Iceberg incremental ETL" --region us-east-1

# 定时触发 Crawler（频率按需调整: 每小时/每天）
aws glue create-trigger --name etl-scheduled-trigger --type SCHEDULED \
  --schedule "cron(0 * * * ? *)" --workflow-name etl-iceberg-workflow \
  --actions '[{"CrawlerName":"etl-mysql-crawler"}]' --region us-east-1

# Crawler 成功后触发 Iceberg Job
aws glue create-trigger --name etl-after-crawler-trigger --type CONDITIONAL \
  --workflow-name etl-iceberg-workflow \
  --predicate '{"Conditions":[{"CrawlerName":"etl-mysql-crawler","LogicalOperator":"EQUALS","CrawlState":"SUCCEEDED"}]}' \
  --actions '[{"JobName":"etl-mysql-to-iceberg"}]' --region us-east-1

# 激活
aws glue start-trigger --name etl-scheduled-trigger --region us-east-1
aws glue start-trigger --name etl-after-crawler-trigger --region us-east-1
```

### 11.2 Redshift 定时同步

Glue Job 完成后，需要触发 Redshift SP 同步数据。两种方式：

**方式 A: Redshift Query Scheduler（推荐，最简单）**

在 Redshift Query Editor V2 中创建 Scheduled Query：
```sql
CALL public.sp_sync_from_iceberg();
```

**方式 B: EventBridge + Glue Job 状态变更触发 Lambda**

Glue Job 成功后通过 EventBridge 触发 Lambda 调用 Redshift Data API 执行 SP。

### 11.3 完整自动化流程

```
cron 定时触发
    │
    ▼ Crawler (更新 MySQL 元数据)
    │ SUCCEEDED
    ▼ Glue Job etl-mysql-to-iceberg (增量 + 脱敏 + MERGE)
    │ SUCCEEDED
    ▼ Redshift SP sp_sync_from_iceberg() (增量 MERGE 到本地表)
    │
    ▼ 物化视图 AUTO REFRESH (mv_user_base, mv_city_stats, mv_monthly_stats)
    │
    ▼ 查询就绪
```

---

## Step 12: 验证

### 12.0 端到端验证脚本

每步做完后可运行验证脚本检查所有资源状态：

```bash
bash etl/verify_workshop.sh
```

### 12.1 插入更新 + 新增数据

使用 [`glue_incremental_test.py`](glue_incremental_test.py) 或 [`glue_add_duplicates.py`](glue_add_duplicates.py)

### 12.2 运行 ETL + 同步

```bash
# Glue Job (MySQL → Iceberg)
aws glue start-job-run --job-name etl-mysql-to-iceberg --region us-east-1

# 等 Job 完成后，Redshift 同步
# CALL public.sp_sync_from_iceberg();
```

### 12.3 预期结果

```
Glue 日志: Extracted 5 incremental records (不是全量)
Iceberg:   15 条 (去重后)
本地表:    15 条 (MERGE 同步)
维度视图:  15 条 (字段拼接、分群正确)
```

---

## 数据保障

| 层 | 机制 | 保障 |
|----|------|------|
| MySQL → Glue | Bookmark `updated_at` | 不漏 INSERT 和 UPDATE |
| Glue → Iceberg | MERGE ON id | 源头去重，每个 id 只保留最新 |
| Iceberg → Redshift | SP MERGE ON id + `>=` 水位线 | 幂等，不丢不重，可重跑 |

## 安全检查

| 资源 | 公网 | 验证 |
|------|------|------|
| RDS | ❌ | `PubliclyAccessible=false` |
| Redshift | ❌ | `PubliclyAccessible=false` |
| S3 | ❌ | 4 项 PublicAccessBlock=true |
| 安全组 | ❌ | 无 0.0.0.0/0 入站规则 |

## 故障排查

| 问题 | 解决 |
|------|------|
| Lake Formation permission | Step 6 授权 |
| SCHEMA_NOT_FOUND | 确认 `--conf` 中 `glue_catalog` 配置 |
| warehousePath null | `--conf` 加 `spark.sql.catalog.glue_catalog.warehouse` |
| Bookmark 不生效 | 检查 transformation_ctx + job.commit() |
| SP 无新数据 | 确认 Glue Job 已运行，Iceberg 有新数据 |
