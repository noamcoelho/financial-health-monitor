"""Executa o pipeline completo sem Airflow.

Uso:
    python scripts/run_pipeline.py
    python scripts/run_pipeline.py --date 2026-10-16
    python scripts/run_pipeline.py --date 2026-10-16 --run-id manual__reprocesso
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime

from financial_monitor.config.settings import get_settings
from financial_monitor.pipeline.steps import run_all
from financial_monitor.utils.dates import market_today
from financial_monitor.utils.logger import setup_logging

logger = logging.getLogger("run_pipeline")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Monitor de Saúde Financeira: pipeline ETL")
    parser.add_argument(
        "--date",
        type=date.fromisoformat,
        default=None,
        help="Data de referência (AAAA-MM-DD). Padrão: hoje no fuso do mercado.",
    )
    parser.add_argument(
        "--run-id",
        default=None,
        help="Identificador da execução. Padrão: manual__<data e hora atuais>.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    settings = get_settings()
    setup_logging(settings.log_level)

    reference_date = args.date or market_today(settings.market_timezone)
    run_id = args.run_id or f"manual__{datetime.now():%Y%m%dT%H%M%S}"

    try:
        result = run_all(run_id, reference_date)
    except Exception:
        logger.exception("Pipeline interrompido. Detalhes acima e em pipeline_runs.")
        return 1

    logger.info(
        "Pipeline concluído: status=%s, %d registro(s) carregados",
        result["status"], result["rows_loaded"],
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())