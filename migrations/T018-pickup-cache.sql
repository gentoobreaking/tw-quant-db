-- T018: Add pickup.cache table for DiskCache PostgreSQL backend

CREATE TABLE IF NOT EXISTS pickup.cache (
    key TEXT PRIMARY KEY,
    data TEXT,
    ts  REAL
);

COMMENT ON TABLE pickup.cache IS 'Cache for tw-quant-pickup HTTP/API responses (PostgreSQL backend for DiskCache)';
