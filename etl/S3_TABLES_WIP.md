# S3 Tables 方案调研笔记（未完成）

## 状态：Glue 写入 ✅ | Redshift 读取 ❌

## 已验证通过

- Glue 5.0 通过 AWS analytics integration 写入 S3 Tables ✅
- MERGE INTO 去重 ✅
- 增量抽取 (bookmark) ✅
- PII 脱敏 ✅

### Glue 关键配置

```python
spark.conf.set("spark.sql.catalog.s3t", "org.apache.iceberg.spark.SparkCatalog")
spark.conf.set("spark.sql.catalog.s3t.catalog-impl", "org.apache.iceberg.aws.glue.GlueCatalog")
spark.conf.set("spark.sql.catalog.s3t.glue.id", "<account>:s3tablescatalog/<table-bucket-name>")
spark.conf.set("spark.sql.catalog.s3t.warehouse", "s3://<table-bucket-name>/warehouse/")
```

### 额外 IAM 需求
- `AmazonS3TablesFullAccess` 托管策略
- Lake Formation: 对 `s3tablescatalog/<bucket>` 的 Catalog + Database + Table 权限

## 未通过：Redshift Provisioned Cluster 读取 S3 Tables

### 问题
Redshift provisioned cluster 通过 Data API (db-user 认证) 无法查询 S3 Tables 的嵌套 catalog。

### 尝试过的方式

| 方式 | 结果 |
|------|------|
| `CATALOG_ID '<account>:s3tablescatalog/<bucket>'` | Schema 创建成功，查询报 `Database etl not found` |
| `CATALOG_ID '<account>:s3tablescatalog'` | Schema 创建成功，查询报 `Database etl not found` |
| `CATALOG_ID '<account>:etl_iceberg_catalog'` (federated) | 同上 |
| 三段式 `s3tablescatalog."bucket".etl.table` | `Could not find parent table` |
| `awsdatacatalog."s3tablescatalog/bucket".etl.table` | `not authenticated with IAM credentials` |

### 根因
Redshift provisioned cluster 的 Spectrum 不支持嵌套 catalog 路径。
AWS 文档说 Redshift 支持 S3 Tables，但可能仅限于：
- Redshift Serverless（原生 IAM 认证）
- Redshift Query Editor V2（自动处理 IAM）
- Provisioned cluster 需要配置 IAM 认证而非 db-user

### 后续验证方向
1. 尝试 Redshift Serverless
2. 尝试 provisioned cluster 开启 IAM 认证
3. 等 AWS 更新 Spectrum 对嵌套 catalog 的支持

## 结论
当前生产环境建议继续使用 **S3 Iceberg 方案**（README.md 中的方案），等 Redshift 对 S3 Tables 的支持更完善后再迁移。
