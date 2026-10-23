"""Etapas do pipeline, independentes de orquestrador.

Cada etapa recebe e devolve dicionários simples (JSON-serializáveis): o mesmo
contrato usado pelo XCom do Airflow e pelo scripts/run_pipeline.py.
Dados volumosos nunca passam pelos dicionários: ficam em Parquet (staging).

Fluxo:
    start -> extract -> validate -> transform -> load -> finish
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict
from datetime import date
from typing import Any

from sqlalchemy.engine import Engine

from financial_monitor.config.settings import Settings, get_settings
from financial_monitor.config.tickers import load_companies
from financial_monitor.extract.batch import extract_companies
from financial_monitor.extract.yahoo_finance import YahooFinanceSource
from financial_monitor.load.database import create_db_engine, verify_connection
from financial_monitor.load.loader import load_dataset, record_run_finish, record_run_start
from financial_monitor.pipeline import staging
from financial_monitor.transform.builder import TransformResult, transform_dataset
from financial_monitor.utils.dates import build_extraction_window
from financial_monitor.validate.quality_checks import validate_fundamentals, validate_prices

logger = logging.getLogger(__name__)

StepInfo = dict[str, Any]
Step = Callable[..., StepInfo]


# ----------------------------------------------------------------------
# Infraestrutura
# ----------------------------------------------------------------------
@contextmanager
def _database(settings: Settings) -> Iterator[Engine]:
    """Abre uma engine, verifica a conexão e garante que ela seja liberada."""
    engine = create_db_engine(settings)
    try:
        verify_connection(engine)
        yield engine
    finally:
        engine.dispose()


def _mark_run_failed(run_id: str, step_name: str, exc: Exception) -> None:
    """Registra a falha em pipeline_runs. Nunca levanta exceção própria."""
    engine = create_db_engine(get_settings())
    try:
        record_run_finish(
            engine,
            run_id,
            status="failed",
            tickers_ok=0,
            failed_tickers=[],
            rows_loaded=0,
            error_message=f"[{step_name}] {type(exc).__name__}: {exc}",
        )
    finally:
        engine.dispose()


def _record_failure(step_name: str) -> Callable[[Step], Step]:
    """Decorador: se a etapa falhar, registra em pipeline_runs e relança o erro.

    Relançar é essencial: é a exceção que faz o Airflow marcar a tarefa
    como falha e aplicar os retries.
    """

    def decorator(func: Step) -> Step:
        @functools.wraps(func)
        def wrapper(ctx: StepInfo, *args: Any, **kwargs: Any) -> StepInfo:
            try:
                return func(ctx, *args, **kwargs)
            except Exception as exc:
                logger.error("Etapa '%s' falhou: %s", step_name, exc)
                _mark_run_failed(ctx["run_id"], step_name, exc)
                raise

        return wrapper

    return decorator


# ----------------------------------------------------------------------
# Etapas
# ----------------------------------------------------------------------
def start_step(run_id: str, reference_date: date) -> StepInfo:
    """Registra o início da execução e fixa o contexto compartilhado pelas etapas."""
    settings = get_settings()
    tickers = [company.ticker for company in load_companies(settings.tickers_file)]

    with _database(settings) as engine:
        record_run_start(engine, run_id, tickers_requested=len(tickers))

    logger.info(
        "Execução %s iniciada: data de referência %s, %d ticker(s)",
        run_id, reference_date, len(tickers),
    )
    return {"run_id": run_id, "reference_date": reference_date.isoformat(), "tickers": tickers}


@_record_failure("extract")
def extract_step(ctx: StepInfo) -> StepInfo:
    """Extrai preços e fundamentos e grava os dados brutos no staging."""
    settings = get_settings()
    window = build_extraction_window(
        date.fromisoformat(ctx["reference_date"]), settings.extraction_lookback_days
    )
    result = extract_companies(
        YahooFinanceSource.from_settings(settings),
        ctx["tickers"],
        window,
        pause_seconds=settings.api_pause_between_tickers,
    )

    raw_dir = staging.run_dir(settings.data_dir, "raw", ctx["run_id"])
    return {
        "prices_path": staging.write_frame(result.prices, raw_dir / "prices.parquet"),
        "fundamentals_path": staging.write_frame(result.fundamentals, raw_dir / "fundamentals.parquet"),
        "succeeded": result.succeeded,
        "failed": result.failed,
    }


@_record_failure("validate")
def validate_step(ctx: StepInfo, extract_info: StepInfo) -> StepInfo:
    """Valida os dados brutos e decide quais tickers seguem."""
    prices = staging.read_frame(extract_info["prices_path"])
    fundamentals = staging.read_frame(extract_info["fundamentals_path"])

    report = validate_prices(prices)
    validate_fundamentals(fundamentals)
    return asdict(report)


@_record_failure("transform")
def transform_step(ctx: StepInfo, extract_info: StepInfo, validation_info: StepInfo) -> StepInfo:
    """Limpa, calcula indicadores e grava o resultado no staging."""
    settings = get_settings()
    result = transform_dataset(
        staging.read_frame(extract_info["prices_path"]),
        staging.read_frame(extract_info["fundamentals_path"]),
        rejected_tickers=list(validation_info["rejected_tickers"]),
    )

    processed_dir = staging.run_dir(settings.data_dir, "processed", ctx["run_id"])
    return {
        "prices_path": staging.write_frame(result.prices, processed_dir / "prices.parquet"),
        "fundamentals_path": staging.write_frame(
            result.fundamentals, processed_dir / "fundamentals.parquet"
        ),
        "indicators_path": staging.write_frame(
            result.indicators, processed_dir / "indicators.parquet"
        ),
    }


@_record_failure("load")
def load_step(ctx: StepInfo, transform_info: StepInfo) -> StepInfo:
    """Grava os dados processados no PostgreSQL, numa única transação."""
    settings = get_settings()
    result = TransformResult(
        prices=staging.read_frame(transform_info["prices_path"]),
        fundamentals=staging.read_frame(transform_info["fundamentals_path"]),
        indicators=staging.read_frame(transform_info["indicators_path"]),
    )
    companies = load_companies(settings.tickers_file, only_active=False)

    with _database(settings) as engine:
        summary = load_dataset(engine, companies, result)
    return {**asdict(summary), "total_rows": summary.total_rows}


def finish_step(
    ctx: StepInfo, extract_info: StepInfo, validation_info: StepInfo, load_info: StepInfo
) -> StepInfo:
    """Registra o resultado final da execução e limpa o staging antigo."""
    settings = get_settings()
    failed = sorted(set(extract_info["failed"]) | set(validation_info["rejected_tickers"]))
    status = "partial_success" if failed else "success"

    engine = create_db_engine(settings)
    try:
        record_run_finish(
            engine,
            ctx["run_id"],
            status=status,
            tickers_ok=len(ctx["tickers"]) - len(failed),
            failed_tickers=failed,
            rows_loaded=load_info["total_rows"],
        )
    finally:
        engine.dispose()

    removed = staging.purge_old_runs(settings.data_dir)
    if failed:
        logger.warning("Execução concluída com falhas parciais: %s", failed)
    return {
        "status": status,
        "failed_tickers": failed,
        "rows_loaded": load_info["total_rows"],
        "staging_runs_removed": removed,
    }


def run_all(run_id: str, reference_date: date) -> StepInfo:
    """Executa todas as etapas em sequência, com o mesmo contrato do Airflow."""
    ctx = start_step(run_id, reference_date)
    extracted = extract_step(ctx)
    validated = validate_step(ctx, extracted)
    transformed = transform_step(ctx, extracted, validated)
    loaded = load_step(ctx, transformed)
    return finish_step(ctx, extracted, validated, loaded)