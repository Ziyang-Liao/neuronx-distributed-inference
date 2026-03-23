# ETL Pipeline: MySQL → S3 → Redshift

## Architecture

```
RDS MySQL (private)
    │
    ▼ [Glue Crawler - daily 2am UTC]
Glue Data Catalog (metadata)
    │
    ▼ [Glue Job 1: etl-mysql-to-s3]
    │  - Incremental extract (job bookmark)
    │  - PII masking: username → SHA256, email → hash@masked.com
    │
S3 Parquet (masked data)
    │
    ▼ [Glue Job 2: etl-s3-to-redshift]
    │  - Load to staging table
    │
Redshift (private)
    ├── user_data_staging (raw load)
    ├── mv_user_data_deduped (materialized view, dedup by id)
    ├── user_data (final clean table via merge procedure)
    ├── v_user_dimension (dimension view with field concat)
    └── v_monthly_user_stats (aggregation)
```

## Resources Created

| Resource | ID/Name | Access |
|----------|---------|--------|
| VPC | vpc-07288bbb688c103a8 (ab3-vpc) | - |
| Private Subnets | subnet-00963b92b901f7fdb (1a), subnet-03673d71d15c2dc86 (1b) | Private only |
| Security Group | sg-0147a8c2b499bdbd4 | VPC CIDR only |
| S3 Bucket | <your-bucket> | VPC Endpoint, all public blocked |
| S3 VPC Endpoint | vpce-01f894f95fa103342 | Gateway type |
| RDS MySQL | etl-mysql | Private, no public access |
| Redshift | etl-redshift | Private, no public access |
| Glue Connection | etl-mysql-connection, etl-redshift-connection | VPC |
| Glue Crawler | etl-mysql-crawler | - |
| Glue Jobs | etl-mysql-to-s3, etl-s3-to-redshift | - |
| Glue Workflow | etl-pipeline-workflow | Daily 2am UTC |
| IAM Roles | etl-glue-role, etl-redshift-s3-role | Least privilege |

## Security

- All resources in private subnets (no public IP)
- Security group allows only VPC CIDR (13.14.0.0/16)
- S3 bucket: all public access blocked + VPC Gateway Endpoint
- RDS: PubliclyAccessible=false
- Redshift: PubliclyAccessible=false
- No 0.0.0.0/0 inbound rules

## Usage

### 1. Initialize MySQL
```bash
mysql -h <rds-endpoint> -u admin -p<your-password> etl_source < etl/mysql_init.sql
```

### 2. Initialize Redshift
```bash
psql -h <redshift-endpoint> -p 5439 -U admin -d etl_dw < etl/redshift_ddl.sql
```

### 3. Run Pipeline Manually
```bash
# Run crawler first
aws glue start-crawler --name etl-mysql-crawler --region us-east-1

# Or trigger entire workflow
aws glue start-workflow-run --name etl-pipeline-workflow --region us-east-1
```

### 4. After Glue loads data to Redshift, refresh MV and merge
```sql
REFRESH MATERIALIZED VIEW public.mv_user_data_deduped;
CALL public.sp_merge_deduped();
```

### 5. Query dimension data
```sql
SELECT * FROM public.v_user_dimension;
SELECT * FROM public.v_monthly_user_stats;
```

## PII Masking

| Field | Method | Example |
|-------|--------|---------|
| username | SHA-256 (first 16 chars) | `a1b2c3d4e5f6g7h8` |
| email | SHA-256 prefix + @masked.com | `a1b2c3d4@masked.com` |

## Dimension Calculations

Redshift natively supports `||` and `CONCAT()` for field concatenation — no UDF needed.

Built-in dimension fields in `v_user_dimension`:
- `display_name`: username || email concatenation
- `email_domain`: extracted from email
- `created_month/quarter/year`: date dimensions
- `tenure_days`: days since creation
- `user_segment`: New/Active/Veteran classification
- `address_line1/city`: parsed from address

## Files

```
etl/
├── glue_mysql_to_s3.py      # Job 1: MySQL → S3 with masking
├── glue_s3_to_redshift.py   # Job 2: S3 → Redshift staging
├── redshift_ddl.sql          # Tables, MV, procedures, views
├── mysql_init.sql            # Sample source data
└── README.md                 # This file
```
