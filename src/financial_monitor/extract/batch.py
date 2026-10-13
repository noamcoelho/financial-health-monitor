"""Extração de vários tickers, com tolerância a falhas individuais."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

import pandas as pd

from financial_monitor.extract.base import (
    FUNDAMENTAL_COLUMNS,
    PRICE_COLUMNS,
    FinancialDataSource,
)
from financial_monitor.extract.exceptions import ExtractionError
from financial_monitor.utils.dates import ExtractionWindow

logger = logging.getLogger(__name__)


@dataclass
class ExtractionResult:
    """Resultado consolidado da extração de todos os tickers."""

    prices: pd.DataFrame
    fundamentals: pd.DataFrame
    succeeded: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)  # ticker -> motivo

    @property
    def requested(self) -> int:
        return len(self.succeeded) + len(self.failed)


def extract_companies(
    source: FinancialDataSource,
    tickers: Sequence[str],
    window: ExtractionWindow,
    *,
    pause_seconds: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
) -> ExtractionResult:
    """Extrai preços e fundamentos de cada ticker.

    Regras de tolerância a falhas (ver Fase 1):
      - falha nos PREÇOS de um ticker: o ticker é registrado como falho e
        a extração continua com os demais;
      - falha nos FUNDAMENTOS: apenas um aviso; o ticker segue só com preços;
      - falha em TODOS os tickers: levanta ExtractionError e interrompe o pipeline.
    """
    if not tickers:
        raise ValueError("A lista de tickers está vazia")

    logger.info(
        "Iniciando extração de %d ticker(s) de %s a %s via %s",
        len(tickers), window.start, window.end, source.name,
    )

    price_frames: list[pd.DataFrame] = []
    fundamental_frames: list[pd.DataFrame] = []
    succeeded: list[str] = []
    failed: dict[str, str] = {}

    for position, ticker in enumerate(tickers):
        if position > 0 and pause_seconds > 0:
            sleep(pause_seconds)  # gentileza com a API: reduz o risco de rate limit

        try:
            price_frames.append(source.get_prices(ticker, window.start, window.end))
        except ExtractionError as exc:
            logger.warning("%s ignorado nesta execução: %s", ticker, exc)
            failed[ticker] = str(exc)
            continue

        try:
            fundamentals = source.get_fundamentals(ticker)
            if not fundamentals.empty:
                fundamental_frames.append(fundamentals)
        except ExtractionError as exc:
            logger.warning("%s: fundamentos indisponíveis (%s). Seguindo só com preços.", ticker, exc)

        succeeded.append(ticker)

    if not succeeded:
        raise ExtractionError(f"Nenhum ticker foi extraído com sucesso. Falhas: {failed}")

    prices = pd.concat(price_frames, ignore_index=True)
    fundamentals_all = (
        pd.concat(fundamental_frames, ignore_index=True)
        if fundamental_frames
        else pd.DataFrame(columns=FUNDAMENTAL_COLUMNS)
    )

    logger.info(
        "Extração concluída: %d ok, %d com falha, %d registros de preço, %d trimestres",
        len(succeeded), len(failed), len(prices), len(fundamentals_all),
    )
    return ExtractionResult(
        prices=prices.loc[:, PRICE_COLUMNS],
        fundamentals=fundamentals_all,
        succeeded=succeeded,
        failed=failed,
    )