# Na definição do DAG, cada @task retorna um XComArg, que o Airflow troca pelo
# valor real em tempo de execução; os stubs do Airflow não modelam isso.
# pyright: reportArgumentType=false
"""
### Monitor de Saúde Financeira de Empresas Públicas

Pipeline ETL semanal: extrai preços e fundamentos do Yahoo Finance, valida,
calcula indicadores e grava no PostgreSQL.

**Agendamento:** toda sexta-feira às 20:00, horário de São Paulo.
O pregão regular da B3 encerra entre 17h e 18h, conforme a época do ano.
As 20h dão margem para o leilão de fechamento e para a consolidação dos
dados pelo Yahoo Finance.

**Aviso:** o financial_health_score é um indicador educacional e NÃO
constitui recomendação de investimento.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pendulum
from airflow.sdk import dag, task  # Airflow 2.x: from airflow.decorators import dag, task
from airflow.timetables.trigger import CronTriggerTimetable

MARKET_TIMEZONE = "America/Sao_Paulo"

default_args = {
    "owner": "data-engineering",
    "retries": 2,
    "retry_delay": timedelta(minutes=10),
    "retry_exponential_backoff": True,
    "execution_timeout": timedelta(minutes=30),
}


@dag(
    dag_id="financial_health_monitor",
    description="ETL semanal de indicadores financeiros de empresas da B3",
    # Falso positivo do Pylance: get_partition_mapper tem implementação padrão.
    schedule=CronTriggerTimetable("0 20 * * 5", timezone=MARKET_TIMEZONE),  # pyright: ignore[reportAbstractUsage]
    start_date=pendulum.datetime(2026, 1, 1, tz=MARKET_TIMEZONE),
    catchup=False,
    max_active_runs=1,
    default_args=default_args,
    tags=["finance", "etl"],
    doc_md=__doc__,
)
def financial_health_monitor() -> None:
    # Os imports do projeto ficam DENTRO das tarefas: o Airflow reprocessa este
    # arquivo a cada poucos segundos, e importar pandas/yfinance no topo
    # deixaria esse processamento lento.

    @task
    def start_run(**context: Any) -> dict:
        from financial_monitor.pipeline import steps

        logical_date = context.get("logical_date") or pendulum.now(MARKET_TIMEZONE)
        reference_date = pendulum.instance(logical_date).in_timezone(MARKET_TIMEZONE).date()
        return steps.start_step(run_id=context["run_id"], reference_date=reference_date)

    @task
    def extract(ctx: dict) -> dict:
        from financial_monitor.pipeline import steps

        return steps.extract_step(ctx)

    @task
    def validate(ctx: dict, extract_info: dict) -> dict:
        from financial_monitor.pipeline import steps

        return steps.validate_step(ctx, extract_info)

    @task
    def transform(ctx: dict, extract_info: dict, validation_info: dict) -> dict:
        from financial_monitor.pipeline import steps

        return steps.transform_step(ctx, extract_info, validation_info)

    @task(retries=3)
    def load(ctx: dict, transform_info: dict) -> dict:
        from financial_monitor.pipeline import steps

        return steps.load_step(ctx, transform_info)

    @task
    def finish_run(
        ctx: dict, extract_info: dict, validation_info: dict, load_info: dict
    ) -> dict:
        from financial_monitor.pipeline import steps

        return steps.finish_step(ctx, extract_info, validation_info, load_info)

    ctx = start_run()
    extracted = extract(ctx)
    validated = validate(ctx, extracted)
    transformed = transform(ctx, extracted, validated)
    loaded = load(ctx, transformed)
    finish_run(ctx, extracted, validated, loaded)


financial_health_monitor()