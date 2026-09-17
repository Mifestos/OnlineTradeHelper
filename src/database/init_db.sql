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


-- ============================================================================
-- Кастомные профили пользователей
-- ============================================================================

CREATE TABLE IF NOT EXISTS user_profiles (
    id SERIAL PRIMARY KEY,
    profile_name VARCHAR(100) NOT NULL UNIQUE,
    display_name VARCHAR(200) NOT NULL,
    description TEXT,
    model_name VARCHAR(50) NOT NULL,
    optimisation_strategy VARCHAR(50) NOT NULL,
    max_asset_weight DOUBLE PRECISION NOT NULL,
    risk_aversion DOUBLE PRECISION NOT NULL,
    days_to_forecast INT NOT NULL DEFAULT 30,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_user_profiles_name ON user_profiles (profile_name);

-- ============================================================================
-- История оптимизаций
-- ============================================================================

CREATE TABLE IF NOT EXISTS optimization_runs (
    id SERIAL PRIMARY KEY,
    run_at TIMESTAMPTZ DEFAULT NOW(),
    profile_name VARCHAR(100),
    model_name VARCHAR(50) NOT NULL,
    optimisation_strategy VARCHAR(50) NOT NULL,
    tickers TEXT[] NOT NULL,
    weights JSONB NOT NULL,
    cash_weight DOUBLE PRECISION DEFAULT 0.0,
    risk_free_rate DOUBLE PRECISION NOT NULL,
    max_asset_weight DOUBLE PRECISION NOT NULL,
    risk_aversion DOUBLE PRECISION NOT NULL,
    fallback_used BOOLEAN DEFAULT FALSE,
    optimiser_success BOOLEAN DEFAULT TRUE
);

CREATE INDEX IF NOT EXISTS idx_optimization_runs_time 
    ON optimization_runs (run_at DESC);