# S3 Tables 方案调研笔记

## 状态：Glue 写入 ✅ | Redshift Provisioned 读取 ❌（Spectrum 引擎限制）

## Glue 5.0 写入 S3 Tables ✅ 已验证

- 增量抽取 (bookmark by updated_at) ✅
- PII 脱敏 ✅
- MERGE INTO 去重 ✅
- 脚本: [`glue_mysql_to_s3tables.py`](glue_mysql_to_s3tables.py)

### 关键配置

```python
spark.conf.set("spark.sql.catalog.s3t", "org.apache.iceberg.spark.SparkCatalog")
spark.conf.set("spark.sql.catalog.s3t.catalog-impl", "org.apache.iceberg.aws.glue.GlueCatalog")
spark.conf.set("spark.sql.catalog.s3t.glue.id", "<account>:s3tablescatalog/<table-bucket>")
spark.conf.set("spark.sql.catalog.s3t.warehouse", "s3://<table-bucket>/warehouse/")
```

### 额外需求
- IAM: `AmazonS3TablesFullAccess` 托管策略
- Lake Formation: 对 `s3tablescatalog/<bucket>` 的 Catalog + Database + Table ALL 权限
- Glue Job: `--datalake-formats iceberg`

---

## Redshift Provisioned Cluster 读取 S3 Tables ❌

### 官方文档

来源: [CREATE EXTERNAL SCHEMA](https://docs.aws.amazon.com/redshift/latest/dg/r_CREATE_EXTERNAL_SCHEMA.html)

> `CATALOG_ID` can be specified **only if** using **federated identity** (`IAM_ROLE 'SESSION'` 或 `CATALOG_ROLE 'SESSION'`)

### 测试结果

| 配置 | Schema 创建 | 找到 Database | 读取数据 | 问题 |
|------|------------|--------------|---------|------|
| `IAM_ROLE 'role-arn'` + `CATALOG_ID` | ✅ | ❌ | - | CATALOG_ID 被忽略，查默认 catalog |
| `IAM_ROLE 'SESSION'` + `CATALOG_ID` (IAM 认证) | ✅ | ✅ | ❌ | S3 Tables 内部存储凭证错误 |
| `CATALOG_ROLE 'SESSION'` + `CATALOG_ID` | ✅ | - | ❌ | 需要 federated identity 登录 |
| `CATALOG_ROLE 'role-arn'` + `CATALOG_ID` | ✅ | ❌ | - | 同第一种，CATALOG_ID 被忽略 |

### 根因分析

1. **非 SESSION 模式**: Redshift Spectrum 忽略 `CATALOG_ID` 中的嵌套路径，只用 account ID 查默认 Glue Catalog
2. **SESSION 模式**: 能正确解析嵌套 catalog 路径，找到 database 和 table，但 Spectrum 引擎用标准 S3 FileIO 读取 Iceberg metadata，而 S3 Tables 的内部存储桶需要通过 S3 Tables API 访问，导致凭证错误

### 结论

Redshift Provisioned Cluster 的 Spectrum 引擎**目前不支持读取 S3 Tables 的内部存储**。这是 Spectrum 引擎层面的限制，不是配置问题。

AWS 文档列出 Redshift 支持 S3 Tables，可能指的是:
- Redshift Query Editor V2（控制台，可能有特殊处理）
- 未来的 Spectrum 引擎更新

---

## 建议

**当前**: 继续使用 S3 Iceberg 方案（README.md），Redshift Spectrum 完全支持

**未来迁移路径**: 等 AWS 更新 Redshift Spectrum 对 S3 Tables 内部存储的支持后，只需:
1. 改 Glue Job 的 catalog 配置（已有 `glue_mysql_to_s3tables.py`）
2. 改 Redshift 的 external schema 指向 s3tablescatalog
3. 其余不变（SP、MV、View 都不用改）
