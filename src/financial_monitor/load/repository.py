"""Operações de escrita no banco (padrão Repository).

Todas as funções recebem uma Connection JÁ DENTRO de uma transação.
Quem decide onde a transação começa e termina é quem chama (load/loader.py).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator, Sequence
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import MetaData, Table, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection
from sqlalchemy.exc import InvalidRequestError

from financial_monitor.config.tickers import Company
from financial_monitor.load.exceptions import LoadError

logger = logging.getLogger(__name__)

TABLE_NAMES = (
    "companies",
    "stock_prices",
    "financial_statements",
    "financial_indicators",
    "pipeline_runs",
)
RUN_STATUSES = frozenset({"running", "success", "partial_success", "failed"})

# Colunas controladas pelo banco: nunca enviadas pelo pipeline num UPDATE
_DATABASE_MANAGED = frozenset({"id", "created_at", "updated_at"})
DEFAULT_CHUNK_SIZE = 5_000

Record = dict[str, Any]


# ----------------------------------------------------------------------
# Infraestrutura
# ----------------------------------------------------------------------
def reflect_tables(conn: Connection) -> dict[str, Table]:
    """Lê a estrutura das tabelas direto do banco (reflection).

    Assim o schema.sql continua sendo a ÚNICA definição das tabelas:
    não repetimos colunas e tipos em Python.
    """
    metadata = MetaData()
    try:
        metadata.reflect(bind=conn, only=list(TABLE_NAMES))
    except InvalidRequestError as exc:
        raise LoadError(
            "Tabelas do projeto não encontradas. O sql/schema.sql foi aplicado neste banco? "
            f"Detalhe: {exc}"
        ) from exc
    return {name: metadata.tables[name] for name in TABLE_NAMES}


def dataframe_to_records(df: pd.DataFrame, *, date_columns: Iterable[str] = ()) -> list[Record]:
    """Converte um DataFrame em registros que o driver consegue gravar.

    - NaN/NaT viram None (NULL). Sem isso, o PostgreSQL gravaria o valor
      especial NaN, que ele considera MAIOR que qualquer número e que, por
      isso, passaria em constraints como "CHECK (ma_7 > 0)".
    - Tipos numpy viram tipos Python (o psycopg2 não adapta numpy.int64).
    - Colunas de data viram datetime.date (as colunas são DATE no banco).
    """
    if df.empty:
        return []

    prepared = df.copy()
    for column in date_columns:
        prepared[column] = pd.to_datetime(prepared[column]).dt.date

    prepared = prepared.astype(object).where(prepared.notna(), None)
    return [
        {
            str(key): value.item() if isinstance(value, np.generic) else value
            for key, value in row.items()
        }
        for row in prepared.to_dict(orient="records")
    ]


def _chunks(rows: list[Record], size: int) -> Iterator[list[Record]]:
    for start in range(0, len(rows), size):
        yield rows[start : start + size]


def upsert_rows(
    conn: Connection,
    table: Table,
    rows: list[Record],
    *,
    constraint: str,
    key_columns: Sequence[str],
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> int:
    """INSERT ... ON CONFLICT DO UPDATE, atualizando SÓ as linhas que mudaram.

    Equivale a:
        INSERT INTO tabela (...) VALUES (...)
        ON CONFLICT ON CONSTRAINT <constraint> DO UPDATE
        SET col = EXCLUDED.col, ...
        WHERE tabela.col IS DISTINCT FROM EXCLUDED.col OR ...
    """
    if not rows:
        logger.info("%s: nenhum registro para carregar", table.name)
        return 0

    unknown = set(rows[0]) - {column.name for column in table.columns}
    if unknown:
        raise LoadError(f"{table.name}: colunas inexistentes no banco: {sorted(unknown)}")

    update_columns = [
        column.name
        for column in table.columns
        if column.name in rows[0]
        and column.name not in key_columns
        and column.name not in _DATABASE_MANAGED
    ]

    stmt = pg_insert(table)
    if update_columns:
        stmt = stmt.on_conflict_do_update(
            constraint=constraint,
            set_={name: stmt.excluded[name] for name in update_columns},
            where=or_(
                *(table.c[name].is_distinct_from(stmt.excluded[name]) for name in update_columns)
            ),
        )
    else:
        stmt = stmt.on_conflict_do_nothing(constraint=constraint)

    for chunk in _chunks(rows, chunk_size):
        conn.execute(stmt, chunk)

    logger.info("%s: %d registro(s) processados (inseridos ou atualizados)", table.name, len(rows))
    return len(rows)


# ----------------------------------------------------------------------
# Tabelas de dados
# ----------------------------------------------------------------------
def sync_companies(conn: Connection, tables: dict[str, Table], companies: Sequence[Company]) -> int:
    """Sincroniza a tabela companies com o tickers.yaml (inclusive empresas inativas)."""
    rows = [
        {
            "ticker": company.ticker,
            "company_name": company.name,
            "sector": company.sector,
            "currency": company.currency,
            "is_active": company.active,
        }
        for company in companies
    ]
    return upsert_rows(
        conn, tables["companies"], rows,
        constraint="uq_companies_ticker", key_columns=("ticker",),
    )


def fetch_company_ids(conn: Connection, tables: dict[str, Table]) -> dict[str, int]:
    """Mapa ticker -> id, usado para trocar o ticker pela chave estrangeira."""
    companies = tables["companies"]
    result = conn.execute(select(companies.c.ticker, companies.c.id))
    return {ticker: company_id for ticker, company_id in result}


def upsert_stock_prices(conn: Connection, tables: dict[str, Table], prices: pd.DataFrame) -> int:
    rows = dataframe_to_records(prices, date_columns=("trade_date",))
    return upsert_rows(
        conn, tables["stock_prices"], rows,
        constraint="uq_stock_prices_company_date", key_columns=("company_id", "trade_date"),
    )


def upsert_financial_statements(
    conn: Connection, tables: dict[str, Table], statements: pd.DataFrame
) -> int:
    rows = dataframe_to_records(statements, date_columns=("period_end", "available_from"))
    return upsert_rows(
        conn, tables["financial_statements"], rows,
        constraint="uq_fin_statements_company_period", key_columns=("company_id", "period_end"),
    )


def upsert_financial_indicators(
    conn: Connection, tables: dict[str, Table], indicators: pd.DataFrame
) -> int:
    rows = dataframe_to_records(indicators, date_columns=("trade_date",))
    return upsert_rows(
        conn, tables["financial_indicators"], rows,
        constraint="uq_indicators_company_date", key_columns=("company_id", "trade_date"),
    )


# ----------------------------------------------------------------------
# Auditoria de execuções
# ----------------------------------------------------------------------
def start_run(
    conn: Connection, tables: dict[str, Table], run_id: str, tickers_requested: int
) -> None:
    """Registra o início de uma execução. Reexecutar o mesmo run_id reinicia o registro."""
    runs = tables["pipeline_runs"]
    stmt = pg_insert(runs).values(
        run_id=run_id,
        started_at=func.now(),
        finished_at=None,
        status="running",
        tickers_requested=tickers_requested,
        tickers_ok=0,
        tickers_failed=0,
        failed_tickers=[],
        rows_loaded=0,
        error_message=None,
    )
    reset_columns = [c.name for c in runs.columns if c.name != "run_id"]
    stmt = stmt.on_conflict_do_update(
        index_elements=[runs.c.run_id],
        set_={name: stmt.excluded[name] for name in reset_columns},
    )
    conn.execute(stmt)


def finish_run(
    conn: Connection,
    tables: dict[str, Table],
    run_id: str,
    *,
    status: str,
    tickers_ok: int,
    failed_tickers: Sequence[str],
    rows_loaded: int,
    error_message: str | None = None,
) -> None:
    """Registra o término de uma execução com seu resultado."""
    if status not in RUN_STATUSES:
        raise ValueError(f"Status inválido: {status!r}. Use um de {sorted(RUN_STATUSES)}")

    runs = tables["pipeline_runs"]
    result = conn.execute(
        update(runs)
        .where(runs.c.run_id == run_id)
        .values(
            finished_at=func.now(),
            status=status,
            tickers_ok=tickers_ok,
            tickers_failed=len(failed_tickers),
            failed_tickers=list(failed_tickers),
            rows_loaded=rows_loaded,
            error_message=error_message[:4000] if error_message else None,
        )
    )
    if result.rowcount == 0:
        logger.warning("Execução %s não encontrada em pipeline_runs", run_id)