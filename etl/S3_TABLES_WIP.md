# S3 Tables 方案调研笔记

## 结论

| 环节 | 状态 | 说明 |
|------|------|------|
| Glue 5.0 写入 S3 Tables | ✅ | MERGE INTO 去重、增量、脱敏全部通过 |
| Athena 查询 S3 Tables | ✅ | 原生支持 |
| Redshift Spectrum 查询 S3 Tables | ❌ | S3 Tables 内部桶禁止标准 S3 API，Spectrum 引擎不支持 |

**当前建议**: 继续使用 S3 Iceberg 方案（README.md）

---

## Glue 写入 S3 Tables ✅

脚本: [`glue_mysql_to_s3tables.py`](glue_mysql_to_s3tables.py)

配置方式: AWS analytics integration（GlueCatalog + `glue.id` 指向 `s3tablescatalog/<bucket>`）

---

## Redshift Spectrum 查询 S3 Tables ❌

### Federated Identity 配置（已完成）

1. IAM Identity Center 已启用 ✅
2. Redshift IDC Application 已创建（`etl-redshift-idc`）✅
3. Lake Formation 集成已启用（`LakeFormationQuery: Enabled`）✅
4. IAM 用户添加 `RedshiftDbUser` tag ✅
5. `IAM_ROLE 'SESSION'` + `CATALOG_ID` 能找到 S3 Tables 的 database 和 table ✅

### 最终失败点

Spectrum 引擎找到 Iceberg metadata 路径后，用标准 S3 `GetObject` 读取数据文件。
但 S3 Tables 内部桶（`s3://xxx--table-s3/`）**禁止标准 S3 API**：

```
$ aws s3 ls s3://5b1c7aca-...--table-s3/
ERROR: MethodNotAllowed - The specified method is not allowed against this resource.
```

这是 S3 Tables 的设计 — 内部桶只能通过 S3 Tables 专用 API 访问，不支持标准 S3 GetObject。
Athena 有独立的 S3 Tables 集成路径（不走标准 S3 API），所以 Athena 能查。
Spectrum 引擎目前只支持标准 S3 FileIO，需要 AWS 更新引擎。

### 测试记录

| 步骤 | 结果 |
|------|------|
| Federated identity (RedshiftDbUser tag) | ✅ 认证成功 |
| `IAM_ROLE SESSION` + `CATALOG_ID` | ✅ 找到 DB + Table |
| 读取 Iceberg metadata | ❌ `Invalid resource arn` (S3 Tables 内部桶禁止标准 S3 API) |
| 直接 `aws s3 ls` S3 Tables 内部桶 | ❌ `MethodNotAllowed` |
