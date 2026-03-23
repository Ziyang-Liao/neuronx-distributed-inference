-- ============================================================
-- Redshift DDL: Tables, Materialized Views, Dimension Queries
-- ============================================================

-- 1. Create staging table (Glue writes here)
CREATE TABLE IF NOT EXISTS public.user_data_staging (
    id              BIGINT,
    username        VARCHAR(256),
    email           VARCHAR(256),
    phone           VARCHAR(64),
    address         VARCHAR(512),
    created_at      TIMESTAMP,
    updated_at      TIMESTAMP,
    _etl_processed_at TIMESTAMP
)
DISTSTYLE AUTO
SORTKEY(id, updated_at);

-- 2. Create final deduped table
CREATE TABLE IF NOT EXISTS public.user_data (
    id              BIGINT,
    username        VARCHAR(256),
    email           VARCHAR(256),
    phone           VARCHAR(64),
    address         VARCHAR(512),
    created_at      TIMESTAMP,
    updated_at      TIMESTAMP,
    _etl_processed_at TIMESTAMP
)
DISTSTYLE AUTO
SORTKEY(id);

-- 3. Materialized View: dedup by id, keep latest record
-- ROW_NUMBER window function to pick the most recent version per id
CREATE MATERIALIZED VIEW public.mv_user_data_deduped AS
SELECT
    id,
    username,
    email,
    phone,
    address,
    created_at,
    updated_at,
    _etl_processed_at
FROM (
    SELECT *,
        ROW_NUMBER() OVER (PARTITION BY id ORDER BY updated_at DESC, _etl_processed_at DESC) AS rn
    FROM public.user_data_staging
)
WHERE rn = 1;

-- Refresh materialized view (run periodically or after each ETL load)
-- REFRESH MATERIALIZED VIEW public.mv_user_data_deduped;

-- 4. Merge deduped data into final table (run after MV refresh)
-- This is a stored procedure for incremental merge
CREATE OR REPLACE PROCEDURE public.sp_merge_deduped()
AS $$
BEGIN
    -- Delete existing records that have updates
    DELETE FROM public.user_data
    USING public.mv_user_data_deduped mv
    WHERE public.user_data.id = mv.id;

    -- Insert all deduped records
    INSERT INTO public.user_data
    SELECT id, username, email, phone, address, created_at, updated_at, _etl_processed_at
    FROM public.mv_user_data_deduped;

    -- Truncate staging after merge
    TRUNCATE TABLE public.user_data_staging;
END;
$$ LANGUAGE plpgsql;

-- ============================================================
-- 5. Dimension Calculation Examples
-- ============================================================

-- Redshift natively supports CONCAT / || operator, no UDF needed.
-- Examples:

-- 5a. Dimension view: user profile with concatenated fields
CREATE OR REPLACE VIEW public.v_user_dimension AS
SELECT
    id,
    username,
    email,
    -- Field concatenation using || (native, no UDF needed)
    username || ' <' || email || '>' AS display_name,
    -- Extract domain from masked email
    SPLIT_PART(email, '@', 2) AS email_domain,
    -- Date dimensions
    DATE_TRUNC('month', created_at) AS created_month,
    DATE_TRUNC('quarter', created_at) AS created_quarter,
    EXTRACT(YEAR FROM created_at) AS created_year,
    EXTRACT(DOW FROM created_at) AS created_day_of_week,
    -- Tenure calculation
    DATEDIFF(day, created_at, GETDATE()) AS tenure_days,
    CASE
        WHEN DATEDIFF(day, created_at, GETDATE()) < 30 THEN 'New'
        WHEN DATEDIFF(day, created_at, GETDATE()) < 365 THEN 'Active'
        ELSE 'Veteran'
    END AS user_segment,
    -- Address parsing (if address contains comma-separated parts)
    SPLIT_PART(address, ',', 1) AS address_line1,
    SPLIT_PART(address, ',', 2) AS city,
    updated_at
FROM public.user_data;

-- 5b. Aggregation dimension: monthly user stats
CREATE OR REPLACE VIEW public.v_monthly_user_stats AS
SELECT
    DATE_TRUNC('month', created_at) AS month,
    COUNT(*) AS total_users,
    COUNT(DISTINCT email_domain) AS unique_domains
FROM public.v_user_dimension
GROUP BY 1
ORDER BY 1;

-- ============================================================
-- Note: Redshift supports || and CONCAT() natively.
-- CONCAT(a, b, c) or a || b || c both work.
-- No UDF is needed for field concatenation.
-- ============================================================
