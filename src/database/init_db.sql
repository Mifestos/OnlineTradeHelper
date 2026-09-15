CREATE TABLE IF NOT EXISTS candles (
    time TIMESTAMPTZ NOT NULL,
    ticker VARCHAR(10) NOT NULL,
    open DOUBLE PRECISION NOT NULL,
    close DOUBLE PRECISION NOT NULL,
    high DOUBLE PRECISION NOT NULL,
    low DOUBLE PRECISION NOT NULL,
    volume INT NOT NULL,
    PRIMARY KEY (time, ticker)
);

SELECT create_hypertable('candles', 'time', chunk_time_interval => INTERVAL '7 days', if_not_exists => TRUE);

CREATE INDEX IF NOT EXISTS idx_ticker_time ON candles (ticker, time DESC);
