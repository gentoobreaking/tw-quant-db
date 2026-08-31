# Phase 3 Cleanup & Optimization SOP

## Overview

Phase 3 of the tw-quant-db shared PostgreSQL migration involves:
1. Dropping compatibility views (`core.v_*_stock`, `selector.v_*_stock`)
2. Setting up monthly range partitioning on `core.daily_prices` (deferred until >1M rows)
3. Creating service account roles and setting permissions
4. Creating backup script and disk usage alert mechanism
5. Setting default `search_path` on the database

## Prerequisites

- PostgreSQL `twquant_shared` database must be running and accessible
- User `twquant` must have superuser privileges
- `postgresql://twquant:twquant-secret-password@localhost:5432/twquant_shared` connection URL available
- Python 3.11+ with `asyncpg` installed

## Pre-flight Checklist

1. **Verify no `stock_id` references in selector backend code**:
   ```bash
   grep -rn "stock_id" tw-quant-selector/backend/ --include="*.py"
   ```
   Must return no results.

2. **Check data volume** for partitioning decision:
   ```sql
   SELECT count(*) FROM core.daily_prices;
   ```
   If < 1,000,000 rows, partitioning is deferred per spec.

3. **Verify service account roles** don't already exist:
   ```sql
   SELECT rolname FROM pg_roles WHERE rolname LIKE 'twquant_%';
   ```

## Execution Steps

### Step 1: Dry Run

Always run in dry-run mode first:

```bash
cd ~/Projects/tw-quant-db
DATABASE_URL="postgresql://twquant:twquant-secret-password@localhost:5432/twquant_shared" \
python3 scripts/phase3_cleanup.py
```

Review the output for:
- All views listed should match `VIEWS_TO_DROP`
- Partitioning should be skipped if <1M rows
- Permission statements printed
- Backup script creation confirmed

### Step 2: Apply

```bash
cd ~/Projects/tw-quant-db
DATABASE_URL="postgresql://twquant:twquant-secret-password@localhost:5432/twquant_shared" \
python3 scripts/phase3_cleanup.py --apply
```

This executes:
1. `DROP VIEW IF EXISTS` for each compatibility view
2. Partition setup (skipped if <1M rows, logged)
3. Role creation + permission grants via single `conn.execute(PERMISSIONS_SQL)` block
4. Backup script creation at `scripts/pg_dump_twquant.sh`
5. `ALTER DATABASE twquant_shared SET search_path TO public, core`

### Step 3: Verification

#### Verify views dropped:
```sql
SELECT table_schema, table_name
FROM information_schema.views
WHERE table_schema IN ('core', 'selector')
AND table_name LIKE 'v_%';
```
Expected: no rows returned.

#### Verify daily_prices data intact:
```sql
SELECT count(*) FROM core.daily_prices;
SELECT count(*) FROM core.daily_prices WHERE symbol = 'GOLD';
```
Expected: data preserved (no `DROP TABLE` on `core.daily_prices`).

#### Verify roles created:
```sql
SELECT rolname, rolcanlogin
FROM pg_roles
WHERE rolname IN ('twquant_readonly', 'twquant_core_writer', 'twquant_pickup', 'twquant_selector', 'twquant_signal', 'twquant_audit_writer')
ORDER BY rolname;
```
Expected: all 6 roles present. `twquant_readonly`, `twquant_core_writer`, `twquant_audit_writer` should be non-login roles (membership roles). `twquant_pickup`, `twquant_selector`, `twquant_signal` should be login roles.

#### Verify permissions:
```sql
-- twquant_readonly should only have SELECT
SELECT privilege_type
FROM information_schema.role_table_grants
WHERE grantee = 'twquant_readonly'
LIMIT 5;

-- twquant_core_writer should have SELECT, INSERT, UPDATE, DELETE
SELECT privilege_type
FROM information_schema.role_table_grants
WHERE grantee = 'twquant_core_writer'
LIMIT 10;
```

#### Verify search_path:
```sql
SHOW search_path;
```
Expected: `public, core`

#### Verify backup script:
```bash
ls -la scripts/pg_dump_twquant.sh
bash -n scripts/pg_dump_twquant.sh  # syntax check
```

## Rollback

There is no automated rollback. Key manual steps:

1. **Recreate dropped views**: Re-run the T001 (core views) and T004 (selector views) migration scripts:
   ```bash
   psql $DATABASE_URL -f core/schema.sql
   psql $DATABASE_URL -f selector/schema/views.sql
   ```

2. **Drop service account roles** (if needed):
   ```sql
   DROP ROLE IF EXISTS twquant_readonly;
   DROP ROLE IF EXISTS twquant_core_writer;
   DROP ROLE IF EXISTS twquant_pickup;
   DROP ROLE IF EXISTS twquant_selector;
   DROP ROLE IF EXISTS twquant_signal;
   DROP ROLE IF EXISTS twquant_audit_writer;
   ```

3. **Revert search_path**:
   ```sql
   ALTER DATABASE twquant_shared SET search_path TO DEFAULT;
   ```

## Backup Script Usage

```bash
# Set environment variables (password required)
export DB_USER=twquant
export DB_PASSWORD=twquant-secret-password
export PGPASSWORD=twquant-secret-password

# Run backup
./scripts/pg_dump_twquant.sh

# The script:
# 1. Creates full database dump in $BACKUP_DIR (default: /backups/twquant)
# 2. Creates incremental backup of core tables (last 1 day)
# 3. Verifies full backup with pg_restore --list
# 4. Deletes backups older than 7 days
# 5. Alerts if disk usage > 80%
```

### Cron Setup

```cron
# Daily backup at 2 AM
0 2 * * * /Users/david/Projects/tw-quant-db/scripts/pg_dump_twquant.sh >> /var/log/twquant-backup.log 2>&1
```

## Partitioning Notes

The `PARTITION_SQL` contains a full table rebuild:
1. Creates `core.daily_prices_new` as `PARTITION BY RANGE (trade_date)`
2. Creates 48 monthly partitions (24 months past + 24 months future)
3. Copies all existing data via `INSERT INTO ... SELECT * FROM core.daily_prices`
4. Drops the old table and renames the new one
5. Creates BRIN index on `trade_date`

**This operation is NOT idempotent and will destroy data if run twice.** Always run in dry-run first. Partitioning is currently deferred because `core.daily_prices` has ~10K rows (< 1M threshold per spec).

## CI Impact

Core schema changes trigger CI tests across all dependent projects:
- `tw-quant-pickup`: 735 tests must pass
- `tw-quant-selector`: API tests
- `gold-analysis`: API tests

Verify all CI pipelines pass before and after Phase 3 changes.
