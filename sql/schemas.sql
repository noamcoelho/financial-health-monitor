-- =====================================================================
-- Monitor de Saúde Financeira de Empresas Públicas
-- Schema do banco de dados do projeto (PostgreSQL 16)
--
-- Este script é IDEMPOTENTE: pode ser executado várias vezes sem erro
-- e sem duplicar objetos (IF NOT EXISTS / OR REPLACE em tudo).
--
-- Executado automaticamente na primeira inicialização do container
-- postgres-data, via /docker-entrypoint-initdb.d/.
-- =====================================================================

BEGIN;

-- ---------------------------------------------------------------------
-- Função utilitária: mantém updated_at sempre correto em qualquer UPDATE,
-- inclusive nos UPDATEs disparados por INSERT ... ON CONFLICT DO UPDATE.
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$;


-- =====================================================================
-- companies: cadastro das empresas monitoradas
-- Fonte da verdade: config/tickers.yaml (sincronizado pelo pipeline)
-- =====================================================================
CREATE TABLE IF NOT EXISTS companies (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ticker        VARCHAR(20)  NOT NULL,
    company_name  VARCHAR(200) NOT NULL,
    sector        VARCHAR(100),
    currency      CHAR(3)      NOT NULL DEFAULT 'BRL',
    is_active     BOOLEAN      NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),

    CONSTRAINT uq_companies_ticker         UNIQUE (ticker),
    CONSTRAINT ck_companies_ticker_upper   CHECK (ticker = UPPER(ticker)),
    CONSTRAINT ck_companies_currency_upper CHECK (currency = UPPER(currency))
);


-- =====================================================================
-- stock_prices: um registro por empresa por pregão
-- =====================================================================
CREATE TABLE IF NOT EXISTS stock_prices (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    company_id    BIGINT        NOT NULL,
    trade_date    DATE          NOT NULL,
    open          NUMERIC(18,6) NOT NULL,
    high          NUMERIC(18,6) NOT NULL,
    low           NUMERIC(18,6) NOT NULL,
    close         NUMERIC(18,6) NOT NULL,
    adj_close     NUMERIC(18,6) NOT NULL,
    volume        BIGINT        NOT NULL,
    dividends     NUMERIC(18,6) NOT NULL DEFAULT 0,
    stock_splits  NUMERIC(12,6) NOT NULL DEFAULT 0,
    created_at    TIMESTAMPTZ   NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ   NOT NULL DEFAULT now(),

    CONSTRAINT fk_stock_prices_company
        FOREIGN KEY (company_id) REFERENCES companies (id) ON DELETE RESTRICT,

    -- Chave natural: garante idempotência (base do ON CONFLICT)
    CONSTRAINT uq_stock_prices_company_date UNIQUE (company_id, trade_date),

    -- Regras de qualidade: preços impossíveis nunca entram no banco
    CONSTRAINT ck_stock_prices_positive
        CHECK (open > 0 AND high > 0 AND low > 0 AND close > 0 AND adj_close > 0),
    CONSTRAINT ck_stock_prices_high_low   CHECK (high >= low),
    CONSTRAINT ck_stock_prices_in_range
        CHECK (open BETWEEN low AND high AND close BETWEEN low AND high),
    CONSTRAINT ck_stock_prices_volume     CHECK (volume >= 0),
    CONSTRAINT ck_stock_prices_dividends  CHECK (dividends >= 0),
    CONSTRAINT ck_stock_prices_splits     CHECK (stock_splits >= 0)
);

-- Consultas do tipo "todas as empresas no dia X" (rankings, último pregão)
CREATE INDEX IF NOT EXISTS ix_stock_prices_trade_date
    ON stock_prices (trade_date);


-- =====================================================================
-- financial_statements: fundamentos trimestrais
-- =====================================================================
CREATE TABLE IF NOT EXISTS financial_statements (
    id                   BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    company_id           BIGINT        NOT NULL,
    period_end           DATE          NOT NULL,
    available_from       DATE          NOT NULL,
    total_revenue        NUMERIC(20,2),
    net_income           NUMERIC(20,2),
    total_assets         NUMERIC(20,2),
    total_liabilities    NUMERIC(20,2),
    stockholders_equity  NUMERIC(20,2),
    revenue_growth_qoq   DOUBLE PRECISION,
    profit_growth_qoq    DOUBLE PRECISION,
    equity_growth_qoq    DOUBLE PRECISION,
    created_at           TIMESTAMPTZ   NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ   NOT NULL DEFAULT now(),

    CONSTRAINT fk_fin_statements_company
        FOREIGN KEY (company_id) REFERENCES companies (id) ON DELETE RESTRICT,
    CONSTRAINT uq_fin_statements_company_period UNIQUE (company_id, period_end),

    -- Um balanço só pode ficar "disponível" depois do fim do trimestre
    CONSTRAINT ck_fin_statements_available CHECK (available_from >= period_end),

    -- Ativos e passivos não podem ser negativos.
    -- Receita, lucro e patrimônio PODEM (prejuízo, passivo a descoberto).
    CONSTRAINT ck_fin_statements_assets      CHECK (total_assets >= 0),
    CONSTRAINT ck_fin_statements_liabilities CHECK (total_liabilities >= 0)
);

-- Busca "último balanço disponível até a data D" (point-in-time)
CREATE INDEX IF NOT EXISTS ix_fin_statements_company_available
    ON financial_statements (company_id, available_from);


-- =====================================================================
-- financial_indicators: indicadores calculados, um por empresa por pregão
-- =====================================================================
CREATE TABLE IF NOT EXISTS financial_indicators (
    id                      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    company_id              BIGINT           NOT NULL,
    trade_date              DATE             NOT NULL,
    daily_return            DOUBLE PRECISION,
    return_90d              DOUBLE PRECISION,
    ma_7                    DOUBLE PRECISION,
    ma_30                   DOUBLE PRECISION,
    ma_90                   DOUBLE PRECISION,
    volatility_30           DOUBLE PRECISION,
    volatility_90           DOUBLE PRECISION,
    revenue_growth_qoq      DOUBLE PRECISION,
    profit_growth_qoq       DOUBLE PRECISION,
    equity_growth_qoq       DOUBLE PRECISION,
    financial_health_score  NUMERIC(5,2),
    score_components_used   SMALLINT,
    created_at              TIMESTAMPTZ      NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ      NOT NULL DEFAULT now(),

    -- FK composta: só existe indicador para um dia que tem preço gravado
    CONSTRAINT fk_indicators_price
        FOREIGN KEY (company_id, trade_date)
        REFERENCES stock_prices (company_id, trade_date)
        ON DELETE CASCADE,
    CONSTRAINT uq_indicators_company_date UNIQUE (company_id, trade_date),

    -- Preço > 0 implica retorno > -100%
    CONSTRAINT ck_indicators_daily_return CHECK (daily_return > -1),
    CONSTRAINT ck_indicators_return_90d   CHECK (return_90d > -1),
    CONSTRAINT ck_indicators_ma_7         CHECK (ma_7 > 0),
    CONSTRAINT ck_indicators_ma_30        CHECK (ma_30 > 0),
    CONSTRAINT ck_indicators_ma_90        CHECK (ma_90 > 0),
    CONSTRAINT ck_indicators_vol_30       CHECK (volatility_30 >= 0),
    CONSTRAINT ck_indicators_vol_90       CHECK (volatility_90 >= 0),
    CONSTRAINT ck_indicators_score_range  CHECK (financial_health_score BETWEEN 0 AND 100),
    CONSTRAINT ck_indicators_components   CHECK (score_components_used BETWEEN 0 AND 5),
    CONSTRAINT ck_indicators_score_needs_components
        CHECK (financial_health_score IS NULL OR score_components_used > 0)
);

CREATE INDEX IF NOT EXISTS ix_indicators_trade_date
    ON financial_indicators (trade_date);


-- =====================================================================
-- pipeline_runs: auditoria de cada execução do pipeline
-- =====================================================================
CREATE TABLE IF NOT EXISTS pipeline_runs (
    run_id             VARCHAR(250) PRIMARY KEY,
    started_at         TIMESTAMPTZ  NOT NULL DEFAULT now(),
    finished_at        TIMESTAMPTZ,
    status             VARCHAR(20)  NOT NULL DEFAULT 'running',
    tickers_requested  INTEGER      NOT NULL DEFAULT 0,
    tickers_ok         INTEGER      NOT NULL DEFAULT 0,
    tickers_failed     INTEGER      NOT NULL DEFAULT 0,
    failed_tickers     TEXT[]       NOT NULL DEFAULT '{}',
    rows_loaded        INTEGER      NOT NULL DEFAULT 0,
    error_message      TEXT,

    CONSTRAINT ck_pipeline_runs_status
        CHECK (status IN ('running', 'success', 'partial_success', 'failed')),
    CONSTRAINT ck_pipeline_runs_finished
        CHECK (finished_at IS NULL OR finished_at >= started_at),
    CONSTRAINT ck_pipeline_runs_counts
        CHECK (tickers_ok >= 0 AND tickers_failed >= 0 AND rows_loaded >= 0
               AND tickers_ok + tickers_failed <= tickers_requested)
);

CREATE INDEX IF NOT EXISTS ix_pipeline_runs_started_at
    ON pipeline_runs (started_at DESC);


-- =====================================================================
-- Triggers de updated_at (CREATE OR REPLACE TRIGGER exige PostgreSQL 14+)
-- =====================================================================
CREATE OR REPLACE TRIGGER trg_companies_updated_at
    BEFORE UPDATE ON companies
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE OR REPLACE TRIGGER trg_stock_prices_updated_at
    BEFORE UPDATE ON stock_prices
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE OR REPLACE TRIGGER trg_fin_statements_updated_at
    BEFORE UPDATE ON financial_statements
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE OR REPLACE TRIGGER trg_indicators_updated_at
    BEFORE UPDATE ON financial_indicators
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();


-- =====================================================================
-- View: foto mais recente de cada empresa ativa (base para dashboards)
-- =====================================================================
CREATE OR REPLACE VIEW vw_latest_indicators AS
SELECT DISTINCT ON (fi.company_id)
    c.ticker,
    c.company_name,
    c.sector,
    fi.trade_date,
    sp.close,
    fi.daily_return,
    fi.return_90d,
    fi.ma_30,
    fi.volatility_30,
    fi.financial_health_score,
    fi.score_components_used
FROM financial_indicators AS fi
JOIN companies    AS c  ON c.id = fi.company_id
JOIN stock_prices AS sp ON sp.company_id = fi.company_id
                       AND sp.trade_date = fi.trade_date
WHERE c.is_active
ORDER BY fi.company_id, fi.trade_date DESC;


-- =====================================================================
-- Documentação dentro do próprio banco (visível com \d+ no psql)
-- =====================================================================
COMMENT ON TABLE companies IS
    'Empresas monitoradas. Sincronizada a partir de config/tickers.yaml.';
COMMENT ON TABLE stock_prices IS
    'Preços diários (OHLCV). Fonte: Yahoo Finance via yfinance.';
COMMENT ON COLUMN stock_prices.adj_close IS
    'Fechamento ajustado por dividendos e splits. Base para retornos e volatilidade.';
COMMENT ON COLUMN financial_statements.available_from IS
    'Data conservadora a partir da qual o balanço é considerado público (evita look-ahead bias).';
COMMENT ON COLUMN financial_indicators.volatility_30 IS
    'Desvio padrão amostral dos retornos diários em 30 pregões, anualizado por raiz de 252.';
COMMENT ON COLUMN financial_indicators.financial_health_score IS
    'Indicador EDUCACIONAL de 0 a 100. Não constitui recomendação de investimento.';
COMMENT ON COLUMN financial_indicators.score_components_used IS
    'Quantos dos 5 componentes estavam disponíveis no cálculo do score.';

COMMIT;