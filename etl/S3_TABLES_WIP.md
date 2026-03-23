# S3 Tables 方案调研笔记

## 结论

| 环节 | 状态 | 说明 |
|------|------|------|
| Glue 5.0 写入 S3 Tables | ✅ | MERGE INTO 去重、增量、脱敏全部通过 |
| Athena 查询 S3 Tables | ✅ | 原生支持（博客已验证） |
| Redshift Spectrum 查询 S3 Tables | ❌ | Spectrum 引擎不支持 S3 Tables 内部存储的凭证获取 |

**当前建议**: 继续使用 S3 Iceberg 方案（README.md），Redshift Spectrum 完全支持。

---

## Glue 写入 S3 Tables ✅

脚本: [`glue_mysql_to_s3tables.py`](glue_mysql_to_s3tables.py)

### 配置（AWS analytics integration 方式，推荐）

```python
spark.conf.set("spark.sql.catalog.s3t", "org.apache.iceberg.spark.SparkCatalog")
spark.conf.set("spark.sql.catalog.s3t.catalog-impl", "org.apache.iceberg.aws.glue.GlueCatalog")
spark.conf.set("spark.sql.catalog.s3t.glue.id", "<account>:s3tablescatalog/<table-bucket>")
spark.conf.set("spark.sql.catalog.s3t.warehouse", "s3://<table-bucket>/warehouse/")
```

### 额外 IAM
- `AmazonS3TablesFullAccess` 托管策略
- Lake Formation: `s3tablescatalog/<bucket>` 的 Catalog + Database + Table ALL

### 测试结果
- 首次全量: 5 条 ✅
- 增量 (UPDATE 1 条 + INSERT 2 条): 3 条 ✅
- MERGE 去重: 总计 7 条 ✅

---

## Redshift Spectrum 查询 S3 Tables ❌

### 详细测试过程

| 步骤 | 配置 | 结果 |
|------|------|------|
| 1 | `IAM_ROLE 'role-arn'` + `CATALOG_ID` + db-user 认证 | CATALOG_ID 被忽略，`Database etl not found` |
| 2 | `IAM_ROLE 'SESSION'` + `CATALOG_ID` + IAM 认证 | 找到 DB 和 table，但读数据报 `Invalid resource arn` |
| 3 | `IAM_ROLE 'role-arn'` + `CATALOG_ROLE 'SESSION'` + IAM 认证 | 找到 DB，但 LF GetDataAccess 报 `Database etl not found` |
| 4 | 在默认 catalog 创建同名 `etl` database | LF 报 `Table user_data not found` |
| 5 | 在默认 catalog 注册同名 table（指向 S3 Tables 内部路径） | LF 报 `Table id mismatch detected` |
| 6 | 注册 S3 Tables 内部桶为 LF data location | `Un-supported resource arn format` |

### 根因

Redshift Spectrum 在读取 Iceberg 数据时，通过 LakeFormation `GetDataAccess` 获取 S3 凭证。
但 Spectrum 引擎在调用 `GetDataAccess` 时**没有传递嵌套 catalog ID**，导致 LF 在默认 catalog 里找不到对应的 database/table，无法颁发凭证。

这是 Spectrum 引擎层面的限制，需要 AWS 更新。

### 参考

- [CREATE EXTERNAL SCHEMA 文档](https://docs.aws.amazon.com/redshift/latest/dg/r_CREATE_EXTERNAL_SCHEMA.html): CATALOG_ID 仅在 SESSION 模式下生效
- [S3 Tables + Redshift 文档](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-tables-integrating-redshift.html): 列出支持但未给出 SQL 示例
- 博客验证查询用的是 Athena，不是 Redshift Spectrum
