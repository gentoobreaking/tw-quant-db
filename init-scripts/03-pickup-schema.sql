-- pickup schema: tw-quant-pickup business logic tables

-- Cache table for tw-quant-pickup DiskCache (PostgreSQL backend)
-- T018: DiskCache PostgreSQL support
CREATE TABLE IF NOT EXISTS pickup.cache (
    key TEXT PRIMARY KEY,
    data TEXT,
    ts  REAL
);

COMMENT ON TABLE pickup.cache IS 'Pickle cache for tw-quant-pickup HTTP/API responses (PostgreSQL backend for DiskCache)';
