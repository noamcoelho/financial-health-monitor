"""Ponto de entrada da transformação: encadeia limpeza, indicadores e score."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass

import pandas as pd

from financial_monitor.transform.cleaning import clean_fundamentals, clean_prices
from financial_monitor.transform.fundamentals import (
    FUNDAMENTAL_OUTPUT_COLUMNS,
    attach_fundamentals,
    compute_fundamental_growth,
)
from financial_monitor.transform.health_score import compute_health_score
from financial_monitor.transform.indicators import (
    WARMUP_TRADING_DAYS,
    compute_price_indicators,
    drop_warmup_rows,
)
from financial_monitor.validate.quality_checks import DataValidationError, validate_indicators

logger = logging.getLogger(__name__)

# Formato final, alinhado com a tabela financial_indicators (ticker vira company_id no Load)
INDICATOR_COLUMNS: list[str] = [
    "ticker",
    "trade_date",
    "daily_return",
    "return_90d",
    "ma_7",
    "ma_30",
    "ma_90",
    "volatility_30",
    "volatility_90",
    "revenue_growth_qoq",
    "profit_growth_qoq",
    "equity_growth_qoq",
    "financial_health_score",
    "score_components_used",
]


class TransformationError(Exception):
    """Falha inesperada durante a transformação (provável bug). Interrompe o pipeline."""


@dataclass
class TransformResult:
    """Dados prontos para o Load."""

    prices: pd.DataFrame        # -> stock_prices
    fundamentals: pd.DataFrame  # -> financial_statements
    indicators: pd.DataFrame    # -> financial_indicators


def transform_dataset(
    prices_raw: pd.DataFrame,
    fundamentals_raw: pd.DataFrame,
    *,
    rejected_tickers: Iterable[str] = (),
    warmup_rows: int = WARMUP_TRADING_DAYS,
) -> TransformResult:
    """Transforma os dados extraídos nos três conjuntos que serão carregados."""
    logger.info("Iniciando transformação")
    rejected = list(rejected_tickers)

    try:
        prices = clean_prices(prices_raw, rejected_tickers=rejected)
        fundamentals = compute_fundamental_growth(
            clean_fundamentals(fundamentals_raw, rejected_tickers=rejected)
        )

        indicators = compute_price_indicators(prices)
        indicators = drop_warmup_rows(indicators, warmup_rows)
        if indicators.empty:
            raise DataValidationError(
                "Nenhum indicador gerado: o histórico extraído é menor que o período "
                "de aquecimento. Aumente EXTRACTION_LOOKBACK_DAYS."
            )
        indicators = attach_fundamentals(indicators, fundamentals)
        indicators = compute_health_score(indicators)
        indicators = indicators.loc[:, INDICATOR_COLUMNS]

        validate_indicators(indicators)

    except DataValidationError:
        raise
    except Exception as exc:
        raise TransformationError(
            f"Falha inesperada na transformação ({type(exc).__name__}: {exc})"
        ) from exc

    logger.info(
        "Transformação concluída: %d preços, %d trimestres, %d indicadores",
        len(prices), len(fundamentals), len(indicators),
    )
    return TransformResult(
        prices=prices,
        fundamentals=fundamentals.loc[:, FUNDAMENTAL_OUTPUT_COLUMNS],
        indicators=indicators,
    )