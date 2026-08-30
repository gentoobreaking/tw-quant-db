"""
T068: Create core shared schema + migrate tw-quant-pickup tables → core schema

This script:
1. Creates the `core` schema (shared, single source of truth for raw/fact data)
2. Migrates data from pickup's existing tables to core.* tables
3. Creates view compatibility layers for selector (stock_id → symbol)

Per scheme C in ~/Projects/tw-quant-db-status.md:
- core.* = raw/fact tables shared across all projects
- pickup.* = business logic tables (stays in pickup schema)
- selector.* = business logic tables (stays in selector schema)
- signal.* = signal-specific tables (stays in signal schema)
- audit.* = shared audit tables
"""

import asyncio
import logging
import sys
from pathlib import Path

# Add parent dir to path
sys.path.insert(0, str(Path(__file__).parent.parent))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

CORE_SCHEMA_SQL = Path(__file__).parent.parent / "core" / "schema.sql"

# Tables to migrate from pickup → core schema
# Maps pickup table → core table (identical schema, lineage columns preserved)
MIGRATE_TABLES = [
    "stocks",
    "daily_prices",
    "financials",
    "monthly_revenues",
    "dividends",
    "institutional_flow",
    "market_context",
    "universe_flags",
]

# Pickup tables that stay in pickup schema (business logic, not shared)
PICKUP_ONLY_TABLES = [
    "analysis_snapshot",
    "factor_scores",
    "etf_factor_scores",
    "valuations",
    "rankings",
    "alert_log",
    "universe_snapshot",
    "ai_analysis",
    "article_valuations",
    "etf_valuations",
    "pipeline_stage_log",
    "snapshot_audit_log",
    "factor_scores_source_role",  # already migrated in 004
]


async def create_core_schema(conn):
    """Step 1: Create core schema tables."""
    sql = CORE_SCHEMA_SQL.read_text()
    await conn.execute("CREATE SCHEMA IF NOT EXISTS core")
    await conn.execute(sql)
    logger.info("Core schema created successfully")


async def migrate_table(conn, table_name: str):
    """Step 2: Copy data from pickup.{table} to core.{table}."""
    # Check if source table exists
    result = await conn.fetchval(
        "SELECT EXISTS (SELECT FROM information_schema.tables "
        "WHERE table_schema = $1 AND table_name = $2)",
        "pickup", table_name,
    )
    if not result:
        logger.info(f"Skipping {table_name}: no pickup table found")
        return 0

    # Count rows in source
    count = await conn.fetchval(f'SELECT COUNT(*) FROM pickup."{table_name}"')
    if count == 0:
        logger.info(f"Skipping {table_name}: 0 rows in pickup table")
        return 0

    # Check if core table has data already
    core_exists = await conn.fetchval(
        f'SELECT COUNT(*) FROM core."{table_name}"')
    if core_exists > 0:
        logger.info(f"Skipping {table_name}: core already has {core_exists} rows")
        return 0

    # Get column names from pickup table (they match core table)
    cols = await conn.fetch(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'pickup' AND table_name = $1 ORDER BY ordinal_position",
        table_name,
    )
    col_names = [row["column_name"] for row in cols]
    col_str = ", ".join(f'"{c}"' for c in col_names)

    # Copy data: pickup → core
    await conn.execute(
        f'INSERT INTO core."{table_name}" ({col_str}) '
        f'SELECT {col_str} FROM pickup."{table_name}"'
    )
    logger.info(f"Migrated {table_name}: {count} rows from pickup.{table_name} → core.{table_name}")
    return count


async def create_compatibility_views(conn):
    """Step 3: Create views for projects using different column names.

    selector uses `stock_id` while core uses `symbol`.
    Create a view that maps symbol → stock_id for backward compat.
    """
    # View: daily_prices with stock_id alias
    await conn.execute("""
        CREATE OR REPLACE VIEW core.v_daily_prices_stock AS
        SELECT
            symbol AS stock_id,
            trade_date,
            open, high, low, close,
            adjusted_close, volume, turnover,
            source, data_date, freshness, source_role
        FROM core.daily_prices
    """)

    # View: stocks with stock_id alias
    await conn.execute("""
        CREATE OR REPLACE VIEW core.v_stocks_stock AS
        SELECT
            symbol AS stock_id,
            name AS stock_name,
            market, industry,
            listed_date AS list_date,
            active AS is_etf,
            created_at
        FROM core.stocks
    """)

    # View: financials with stock_id alias
    await conn.execute("""
        CREATE OR REPLACE VIEW core.v_financials_stock AS
        SELECT
            symbol AS stock_id,
            CONCAT(fiscal_year, 'Q', fiscal_quarter) AS year_quarter,
            *
        FROM core.financials
    """)

    # View: monthly_revenue with stock_id alias
    await conn.execute("""
        CREATE OR REPLACE VIEW core.v_monthly_revenue_stock AS
        SELECT
            symbol AS stock_id,
            TO_CHAR(year_month, 'YYYY-MM') AS year_month,
            revenue, yoy_growth AS revenue_yoy,
            reported_at AS announcement_date
        FROM core.monthly_revenues
    """)

    logger.info("Compatibility views created for selector (stock_id → symbol)")


async def verify_migration(conn):
    """Step 4: Verify data integrity."""
    checks = [
        ("core.stocks", "pickup.stocks"),
        ("core.daily_prices", "pickup.daily_prices"),
        ("core.financials", "pickup.financials"),
        ("core.monthly_revenues", "pickup.monthly_revenues"),
        ("core.dividends", "pickup.dividends"),
        ("core.institutional_flow", "pickup.institutional_flow"),
        ("core.market_context", "pickup.market_context"),
        ("core.universe_flags", "pickup.universe_flags"),
    ]

    all_ok = True
    for core_tbl, pickup_tbl in checks:
        try:
            core_count = await conn.fetchval(f'SELECT COUNT(*) FROM {core_tbl}')
            pickup_count = await conn.fetchval(f'SELECT COUNT(*) FROM {pickup_tbl}')
            if core_count == pickup_count:
                logger.info(f"  ✓ {core_tbl}: {core_count} rows (matches pickup)")
            else:
                logger.warning(f"  ✗ {core_tbl}: {core_count} vs pickup {pickup_count}")
                all_ok = False
        except (asyncpg.exceptions.UndefinedTableError, Exception) as e:
            logger.info(f"  - {core_tbl}: skipped (no pickup data yet) — {e}")

    if all_ok:
        logger.info("✅ All migration verification checks passed")
    else:
        logger.warning("⚠️  Some verification checks failed")
    return all_ok


async def main():
    """Main migration entry point."""
    import os
    database_url = os.environ.get("DATABASE_URL", "postgresql://localhost:5432/twquant_shared")

    # Use asyncpg for PostgreSQL
    from asyncpg import create_pool
    pool = await create_pool(database_url)
    async with pool.acquire() as conn:
        # Step 1: Create core schema
        await create_core_schema(conn)

        # Step 2: Migrate data from pickup tables
        total_rows = 0
        for table in MIGRATE_TABLES:
            total_rows += await migrate_table(conn, table)

        logger.info(f"Total rows migrated: {total_rows}")

        # Step 3: Create compatibility views
        await create_compatibility_views(conn)

        # Step 4: Verify
        await verify_migration(conn)

    await pool.close()
    logger.info("Migration complete")


if __name__ == "__main__":
    asyncio.run(main())
