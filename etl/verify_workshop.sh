#!/bin/bash
# ============================================================
# ETL Workshop 端到端验证脚本
# 逐步检查每个 Step 的资源和状态是否正确
# ============================================================
set -e
REGION="us-east-1"
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

pass() { echo -e "  ${GREEN}✅ $1${NC}"; }
fail() { echo -e "  ${RED}❌ $1${NC}"; ERRORS=$((ERRORS+1)); }
warn() { echo -e "  ${YELLOW}⚠️  $1${NC}"; }
ERRORS=0

echo "============================================================"
echo " ETL Workshop Verification - $(date)"
echo "============================================================"

# ---- Step 1: Network ----
echo ""
echo "== Step 1: Network =="

# 1.1 Subnets
SUBNET_COUNT=$(aws ec2 describe-subnets --filters "Name=tag:Name,Values=etl-private-subnet-1a,etl-private-subnet-1b" --region $REGION --query 'length(Subnets)' --output text 2>/dev/null)
[ "$SUBNET_COUNT" = "2" ] && pass "2 private subnets exist" || fail "Expected 2 subnets, got $SUBNET_COUNT"

PUBLIC_IP=$(aws ec2 describe-subnets --filters "Name=tag:Name,Values=etl-private-subnet-1a" --region $REGION --query 'Subnets[0].MapPublicIpOnLaunch' --output text 2>/dev/null)
[ "$PUBLIC_IP" = "False" ] && pass "Subnet MapPublicIp=false" || fail "Subnet MapPublicIp=$PUBLIC_IP (should be false)"

# 1.3 Security Group
SG_ID=$(aws ec2 describe-security-groups --filters "Name=group-name,Values=etl-pipeline-sg" --region $REGION --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null)
[ -n "$SG_ID" ] && [ "$SG_ID" != "None" ] && pass "Security group exists: $SG_ID" || fail "Security group not found"

OPEN_RULES=$(aws ec2 describe-security-groups --group-ids $SG_ID --region $REGION --query 'SecurityGroups[0].IpPermissions[].IpRanges[?CidrIp==`0.0.0.0/0`]' --output text 2>/dev/null)
[ -z "$OPEN_RULES" ] && pass "No 0.0.0.0/0 inbound rules" || fail "Found 0.0.0.0/0 rule!"

# 1.4 S3 VPC Endpoint
VPCE=$(aws ec2 describe-vpc-endpoints --filters "Name=tag:Name,Values=etl-s3-endpoint" --region $REGION --query 'VpcEndpoints[0].State' --output text 2>/dev/null)
[ "$VPCE" = "available" ] && pass "S3 VPC Endpoint available" || fail "S3 VPC Endpoint state: $VPCE"

# ---- Step 2: RDS MySQL ----
echo ""
echo "== Step 2: RDS MySQL =="

RDS_STATUS=$(aws rds describe-db-instances --db-instance-identifier etl-mysql --region $REGION --query 'DBInstances[0].DBInstanceStatus' --output text 2>/dev/null)
[ "$RDS_STATUS" = "available" ] && pass "RDS status: available" || fail "RDS status: $RDS_STATUS"

RDS_PUBLIC=$(aws rds describe-db-instances --db-instance-identifier etl-mysql --region $REGION --query 'DBInstances[0].PubliclyAccessible' --output text 2>/dev/null)
[ "$RDS_PUBLIC" = "False" ] && pass "RDS PubliclyAccessible=false" || fail "RDS PubliclyAccessible=$RDS_PUBLIC"

RDS_ENDPOINT=$(aws rds describe-db-instances --db-instance-identifier etl-mysql --region $REGION --query 'DBInstances[0].Endpoint.Address' --output text 2>/dev/null)
pass "RDS endpoint: $RDS_ENDPOINT"

# ---- Step 3: S3 ----
echo ""
echo "== Step 3: S3 Bucket =="

BUCKET=$(aws s3api list-buckets --query 'Buckets[?starts_with(Name,`etl-pipeline-data`)].Name|[0]' --output text 2>/dev/null)
[ -n "$BUCKET" ] && [ "$BUCKET" != "None" ] && pass "Bucket exists: $BUCKET" || fail "Bucket not found"

BLOCK=$(aws s3api get-public-access-block --bucket $BUCKET --region $REGION --query 'PublicAccessBlockConfiguration.[BlockPublicAcls,IgnorePublicAcls,BlockPublicPolicy,RestrictPublicBuckets]' --output text 2>/dev/null)
if echo "$BLOCK" | grep -q "False"; then
    fail "S3 public access not fully blocked: $BLOCK"
else
    pass "S3 all public access blocked"
fi

# ---- Step 4: Redshift ----
echo ""
echo "== Step 4: Redshift =="

RS_STATUS=$(aws redshift describe-clusters --cluster-identifier etl-redshift --region $REGION --query 'Clusters[0].ClusterStatus' --output text 2>/dev/null)
[ "$RS_STATUS" = "available" ] && pass "Redshift status: available" || fail "Redshift status: $RS_STATUS"

RS_PUBLIC=$(aws redshift describe-clusters --cluster-identifier etl-redshift --region $REGION --query 'Clusters[0].PubliclyAccessible' --output text 2>/dev/null)
[ "$RS_PUBLIC" = "False" ] && pass "Redshift PubliclyAccessible=false" || fail "Redshift PubliclyAccessible=$RS_PUBLIC"

RS_IAM=$(aws redshift describe-clusters --cluster-identifier etl-redshift --region $REGION --query 'Clusters[0].IamRoles[0].ApplyStatus' --output text 2>/dev/null)
[ "$RS_IAM" = "in-sync" ] && pass "Redshift IAM role in-sync" || fail "Redshift IAM role: $RS_IAM"

# ---- Step 5: IAM Roles ----
echo ""
echo "== Step 5: IAM Roles =="

GLUE_ROLE=$(aws iam get-role --role-name etl-glue-role --query 'Role.Arn' --output text 2>/dev/null)
[ -n "$GLUE_ROLE" ] && pass "Glue role exists" || fail "Glue role not found"

GLUE_MANAGED=$(aws iam list-attached-role-policies --role-name etl-glue-role --query 'AttachedPolicies[?PolicyName==`AWSGlueServiceRole`].PolicyName|[0]' --output text 2>/dev/null)
[ "$GLUE_MANAGED" = "AWSGlueServiceRole" ] && pass "AWSGlueServiceRole attached" || fail "AWSGlueServiceRole not attached"

RS_ROLE=$(aws iam get-role --role-name etl-redshift-s3-role --query 'Role.Arn' --output text 2>/dev/null)
[ -n "$RS_ROLE" ] && pass "Redshift role exists" || fail "Redshift role not found"

# ---- Step 6: Lake Formation ----
echo ""
echo "== Step 6: Lake Formation =="

LF_GLUE=$(aws lakeformation list-permissions --region $REGION --query "PrincipalResourcePermissions[?Principal.DataLakePrincipalIdentifier=='$GLUE_ROLE' && Resource.Database.Name=='etl_catalog_db'].Permissions|[0]|[0]" --output text 2>/dev/null)
[ "$LF_GLUE" = "ALL" ] && pass "Glue role has LF DB permission" || fail "Glue role LF DB permission: $LF_GLUE"

# ---- Step 7: Glue Catalog + Connection + Crawler ----
echo ""
echo "== Step 7: Glue Catalog =="

DB_EXISTS=$(aws glue get-database --name etl_catalog_db --region $REGION --query 'Database.Name' --output text 2>/dev/null)
[ "$DB_EXISTS" = "etl_catalog_db" ] && pass "Glue database exists" || fail "Glue database not found"

CONN=$(aws glue get-connection --name etl-mysql-connection --region $REGION --query 'Connection.Name' --output text 2>/dev/null)
[ "$CONN" = "etl-mysql-connection" ] && pass "MySQL connection exists" || fail "MySQL connection not found"

CRAWLER_STATE=$(aws glue get-crawler --name etl-mysql-crawler --region $REGION --query 'Crawler.State' --output text 2>/dev/null)
[ "$CRAWLER_STATE" = "READY" ] && pass "Crawler state: READY" || fail "Crawler state: $CRAWLER_STATE"

CRAWLER_LAST=$(aws glue get-crawler --name etl-mysql-crawler --region $REGION --query 'Crawler.LastCrawl.Status' --output text 2>/dev/null)
[ "$CRAWLER_LAST" = "SUCCEEDED" ] && pass "Crawler last run: SUCCEEDED" || warn "Crawler last run: $CRAWLER_LAST"

TABLE_COUNT=$(aws glue get-tables --database-name etl_catalog_db --region $REGION --query 'length(TableList)' --output text 2>/dev/null)
pass "Catalog tables: $TABLE_COUNT"

MYSQL_TABLE=$(aws glue get-table --database-name etl_catalog_db --name etl_source_user_data --region $REGION --query 'Table.Name' --output text 2>/dev/null)
[ "$MYSQL_TABLE" = "etl_source_user_data" ] && pass "MySQL table in catalog" || fail "MySQL table not in catalog"

ICEBERG_TABLE=$(aws glue get-table --database-name etl_catalog_db --name user_data_iceberg --region $REGION --query 'Table.Parameters.table_type' --output text 2>/dev/null)
[ "$ICEBERG_TABLE" = "ICEBERG" ] && pass "Iceberg table in catalog (type=ICEBERG)" || fail "Iceberg table not found or wrong type: $ICEBERG_TABLE"

# ---- Step 8: Glue Job ----
echo ""
echo "== Step 8: Glue Job =="

JOB_VERSION=$(aws glue get-job --job-name etl-mysql-to-iceberg --region $REGION --query 'Job.GlueVersion' --output text 2>/dev/null)
[ "$JOB_VERSION" = "5.0" ] && pass "Glue version: 5.0" || fail "Glue version: $JOB_VERSION"

JOB_BOOKMARK=$(aws glue get-job --job-name etl-mysql-to-iceberg --region $REGION --query 'Job.DefaultArguments."--job-bookmark-option"' --output text 2>/dev/null)
[ "$JOB_BOOKMARK" = "job-bookmark-enable" ] && pass "Job bookmark enabled" || fail "Job bookmark: $JOB_BOOKMARK"

JOB_ICEBERG=$(aws glue get-job --job-name etl-mysql-to-iceberg --region $REGION --query 'Job.DefaultArguments."--datalake-formats"' --output text 2>/dev/null)
[ "$JOB_ICEBERG" = "iceberg" ] && pass "Iceberg format enabled" || fail "Datalake formats: $JOB_ICEBERG"

JOB_CONF=$(aws glue get-job --job-name etl-mysql-to-iceberg --region $REGION --query 'Job.DefaultArguments."--conf"' --output text 2>/dev/null)
echo "$JOB_CONF" | grep -q "glue_catalog" && pass "--conf contains glue_catalog config" || fail "--conf missing glue_catalog"
echo "$JOB_CONF" | grep -q "IcebergSparkSessionExtensions" && pass "--conf contains Iceberg extensions" || fail "--conf missing Iceberg extensions"

LAST_RUN=$(aws glue get-job-runs --job-name etl-mysql-to-iceberg --region $REGION --query 'JobRuns[0].JobRunState' --output text 2>/dev/null)
[ "$LAST_RUN" = "SUCCEEDED" ] && pass "Last job run: SUCCEEDED" || warn "Last job run: $LAST_RUN"

# ---- Step 9: Verify Iceberg data ----
echo ""
echo "== Step 9: Iceberg Data =="

ICEBERG_LOC=$(aws glue get-table --database-name etl_catalog_db --name user_data_iceberg --region $REGION --query 'Table.StorageDescriptor.Location' --output text 2>/dev/null)
pass "Iceberg location: $ICEBERG_LOC"

ICEBERG_FILES=$(aws s3 ls "${ICEBERG_LOC}/data/" --region $REGION 2>/dev/null | wc -l)
[ "$ICEBERG_FILES" -gt 0 ] && pass "Iceberg data files: $ICEBERG_FILES" || fail "No Iceberg data files found"

# ---- Step 10: Redshift objects ----
echo ""
echo "== Step 10: Redshift Objects =="

# Use Redshift Data API to check
STMT_ID=$(aws redshift-data execute-statement --cluster-identifier etl-redshift --database etl_dw --db-user admin --region $REGION \
  --sql "SELECT 'iceberg_local' AS obj, COUNT(*) AS cnt FROM public.iceberg_local UNION ALL SELECT 'mv_user_base', COUNT(*) FROM public.mv_user_base UNION ALL SELECT 'mv_city_stats', COUNT(*) FROM public.mv_city_stats UNION ALL SELECT 'mv_monthly_stats', COUNT(*) FROM public.mv_monthly_stats UNION ALL SELECT 'v_user_dimension', COUNT(*) FROM public.v_user_dimension" \
  --query 'Id' --output text 2>/dev/null)

sleep 8

STATUS=$(aws redshift-data describe-statement --id $STMT_ID --region $REGION --query 'Status' --output text 2>/dev/null)
if [ "$STATUS" = "FINISHED" ]; then
    aws redshift-data get-statement-result --id $STMT_ID --region $REGION 2>/dev/null | python3 -c "
import json,sys
d=json.load(sys.stdin)
for r in d['Records']:
    name=list(r[0].values())[0]
    cnt=list(r[1].values())[0]
    status='✅' if cnt > 0 else '❌'
    print(f'  {status} {name}: {cnt} rows')
"
else
    fail "Redshift query failed: $STATUS"
fi

# Check SP exists
SP_ID=$(aws redshift-data execute-statement --cluster-identifier etl-redshift --database etl_dw --db-user admin --region $REGION \
  --sql "SELECT proname FROM pg_proc WHERE proname='sp_sync_from_iceberg'" \
  --query 'Id' --output text 2>/dev/null)
sleep 4
SP_EXISTS=$(aws redshift-data get-statement-result --id $SP_ID --region $REGION --query 'TotalNumRows' --output text 2>/dev/null)
[ "$SP_EXISTS" = "1" ] && pass "SP sp_sync_from_iceberg exists" || fail "SP not found"

# Check MV auto refresh
MV_ID=$(aws redshift-data execute-statement --cluster-identifier etl-redshift --database etl_dw --db-user admin --region $REGION \
  --sql "SELECT name, autorefresh FROM stv_mv_info" \
  --query 'Id' --output text 2>/dev/null)
sleep 4
aws redshift-data get-statement-result --id $MV_ID --region $REGION 2>/dev/null | python3 -c "
import json,sys
d=json.load(sys.stdin)
for r in d['Records']:
    name=str(list(r[0].values())[0]).strip()
    auto=list(r[1].values())[0]
    status='✅' if auto else '⚠️'
    print(f'  {status} {name}: autorefresh={auto}')
"

# Check external schema
ES_ID=$(aws redshift-data execute-statement --cluster-identifier etl-redshift --database etl_dw --db-user admin --region $REGION \
  --sql "SELECT schemaname FROM svv_external_schemas WHERE schemaname='iceberg_schema'" \
  --query 'Id' --output text 2>/dev/null)
sleep 4
ES_EXISTS=$(aws redshift-data get-statement-result --id $ES_ID --region $REGION --query 'TotalNumRows' --output text 2>/dev/null)
[ "$ES_EXISTS" = "1" ] && pass "External schema iceberg_schema exists" || fail "External schema not found"

# ---- Step 11: Workflow ----
echo ""
echo "== Step 11: Workflow =="

WF=$(aws glue get-workflow --name etl-pipeline-workflow --region $REGION --query 'Workflow.Name' --output text 2>/dev/null)
[ "$WF" = "etl-pipeline-workflow" ] && pass "Workflow exists" || warn "Workflow not found (may need to create etl-iceberg-workflow)"

# ---- Summary ----
echo ""
echo "============================================================"
if [ $ERRORS -eq 0 ]; then
    echo -e "${GREEN} ALL CHECKS PASSED ✅${NC}"
else
    echo -e "${RED} $ERRORS CHECK(S) FAILED ❌${NC}"
fi
echo "============================================================"
