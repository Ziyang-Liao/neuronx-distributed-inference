# S3 Tables 方案调研笔记

## 状态：Glue 写入 ✅ | Redshift 读取需要 IAM 认证（非 db-user）

## Glue 5.0 写入 S3 Tables ✅ 已验证

脚本: [`glue_mysql_to_s3tables.py`](glue_mysql_to_s3tables.py)

- 增量抽取 (bookmark by updated_at) ✅
- PII 脱敏 ✅
- MERGE INTO 去重 ✅
- 首次全量 5 条 → 增量 3 条 → 总计 7 条 ✅

## Redshift 查询 S3 Tables

### 根因

Redshift 访问 S3 Tables 需要通过 `CATALOG_ID` 指向嵌套 catalog（`s3tablescatalog/<bucket>`）。
根据官方文档，`CATALOG_ID` **仅在 IAM 认证（federated identity）下生效**。

用 `--db-user admin`（数据库用户认证）时，`CATALOG_ID` 被忽略或报 `not authenticated with IAM credentials`。

### 解决方案

Redshift provisioned cluster 支持 IAM 认证，需要：

1. 在 Redshift 中启用 IAM 认证（通过 IAM Identity Center 或 SAML 联合身份）
2. 用户通过 IAM 身份登录 Redshift（而非数据库用户名密码）
3. 创建 schema 时使用 `IAM_ROLE 'SESSION'` + `CATALOG_ID`

或者通过 **Redshift Query Editor V2**（AWS 控制台），它自动处理 IAM 认证，可以直接选择 s3tablescatalog 下的数据库和表。

### 当前方案建议

**生产环境**: 继续使用 S3 Iceberg 方案（README.md），Redshift Spectrum 完全支持，无认证限制。

**未来升级**: 配置 IAM Identity Center 集成后，可切换到 S3 Tables 方案。
