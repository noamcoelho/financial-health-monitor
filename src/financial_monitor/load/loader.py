"""Orquestra a carga: ordem das tabelas, transação única e auditoria."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass

import pandas as pd
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, OperationalError, SQLAlchemyError

from financial_monitor.config.tickers import Company
from financial_monitor.load.exceptions import DatabaseConnectionError, LoadError
from financial_monitor.load.repository import (
    fetch_company_ids,
    finish_run,
    reflect_tables,
    start_run,
    sync_companies,
    upsert_financial_indicators,
    upsert_financial_statements,
    upsert_stock_prices,
)
from financial_monitor.transform.builder import TransformResult

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LoadSummary:
    """Quantidade de registros processados por tabela."""

    companies: int
    prices: int
    statements: int
    indicators: int

    @property
    def total_rows(self) -> int:
        return self.prices + self.statements + self.indicators


def _attach_company_id(df: pd.DataFrame, company_ids: dict[str, int], dataset: str) -> pd.DataFrame:
    """Troca a coluna ticker pela chave estrangeira company_id."""
    prepared = df.copy()
    prepared["company_id"] = prepared["ticker"].map(company_ids)

    missing = sorted(prepared.loc[prepared["company_id"].isna(), "ticker"].unique())
    if missing:
        raise LoadError(f"{dataset}: tickers sem cadastro na tabela companies: {missing}")

    prepared["company_id"] = prepared["company_id"].astype("int64")
    return prepared.drop(columns="ticker")


def load_dataset(engine: Engine, companies: Sequence[Company], result: TransformResult) -> LoadSummary:
    """Grava todos os dados numa ÚNICA transação: ou tudo entra, ou nada entra.

    Ordem obrigatória (chaves estrangeiras da Fase 3):
      companies -> stock_prices -> financial_statements -> financial_indicators
    financial_indicators depende de stock_prices pela FK composta (company_id, trade_date).
    """
    logger.info("Iniciando carga no PostgreSQL")
    try:
        with engine.begin() as conn:  # COMMIT ao final, ROLLBACK em qualquer exceção
            tables = reflect_tables(conn)

            n_companies = sync_companies(conn, tables, companies)
            company_ids = fetch_company_ids(conn, tables)

            n_prices = upsert_stock_prices(
                conn, tables, _attach_company_id(result.prices, company_ids, "Preços")
            )
            n_statements = upsert_financial_statements(
                conn, tables, _attach_company_id(result.fundamentals, company_ids, "Fundamentos")
            )
            n_indicators = upsert_financial_indicators(
                conn, tables, _attach_company_id(result.indicators, company_ids, "Indicadores")
            )

    except IntegrityError as exc:
        raise LoadError(
            "O banco recusou os dados por violação de constraint. Isso indica um bug "
            f"na validação ou na transformação. Detalhe: {exc.orig}"
        ) from exc
    except OperationalError as exc:
        raise DatabaseConnectionError(f"Conexão perdida durante a carga: {exc.orig or exc}") from exc
    except SQLAlchemyError as exc:
        raise LoadError(f"Falha na carga ({type(exc).__name__}): {exc}") from exc

    summary = LoadSummary(n_companies, n_prices, n_statements, n_indicators)
    logger.info(
        "Dados carregados no PostgreSQL: %d empresas, %d preços, %d trimestres, %d indicadores",
        summary.companies, summary.prices, summary.statements, summary.indicators,
    )
    return summary


def record_run_start(engine: Engine, run_id: str, tickers_requested: int) -> None:
    """Registra o início da execução numa transação própria."""
    with engine.begin() as conn:
        start_run(conn, reflect_tables(conn), run_id, tickers_requested)
    logger.info("Execução %s registrada em pipeline_runs", run_id)


def record_run_finish(
    engine: Engine,
    run_id: str,
    *,
    status: str,
    tickers_ok: int,
    failed_tickers: Sequence[str],
    rows_loaded: int,
    error_message: str | None = None,
) -> None:
    """Registra o término da execução numa transação própria.

    Falhas aqui são apenas registradas no log, sem levantar exceção: a
    auditoria NUNCA deve esconder o erro original que encerrou o pipeline,
    nem desfazer uma carga que já foi concluída com sucesso.
    """
    try:
        with engine.begin() as conn:
            finish_run(
                conn, reflect_tables(conn), run_id,
                status=status,
                tickers_ok=tickers_ok,
                failed_tickers=failed_tickers,
                rows_loaded=rows_loaded,
                error_message=error_message,
            )
        logger.info("Execução %s finalizada com status '%s'", run_id, status)
    except SQLAlchemyError:
        logger.exception("Não foi possível registrar o término da execução %s", run_id)