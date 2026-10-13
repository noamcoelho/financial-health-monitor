"""Contrato comum para qualquer fonte de dados financeiros."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date

import pandas as pd

# Formato padronizado que TODA fonte deve entregar.
# O restante do pipeline depende apenas destas colunas, nunca da API.
PRICE_COLUMNS: list[str] = [
    "ticker",
    "trade_date",
    "open",
    "high",
    "low",
    "close",
    "adj_close",
    "volume",
    "dividends",
    "stock_splits",
]

FUNDAMENTAL_COLUMNS: list[str] = [
    "ticker",
    "period_end",
    "total_revenue",
    "net_income",
    "total_assets",
    "total_liabilities",
    "stockholders_equity",
]


class FinancialDataSource(ABC):
    """Interface de uma fonte de dados financeiros."""

    name: str = "base"

    @abstractmethod
    def get_prices(self, ticker: str, start: date, end: date) -> pd.DataFrame:
        """Retorna preços diários de ``ticker`` entre ``start`` e ``end`` (inclusivos).

        O DataFrame DEVE conter exatamente as colunas de PRICE_COLUMNS, com
        ``trade_date`` sem timezone e normalizado para meia-noite.

        Raises:
            EmptyDataError: nenhum pregão encontrado.
            TransientExtractionError: falha temporária que persistiu após os retries.
            InvalidResponseError: resposta fora do formato esperado.
        """

    @abstractmethod
    def get_fundamentals(self, ticker: str) -> pd.DataFrame:
        """Retorna dados trimestrais com as colunas de FUNDAMENTAL_COLUMNS.

        Ausência de fundamentos NÃO é erro: retorna um DataFrame vazio
        com as colunas esperadas.
        """

    @staticmethod
    def validate_period(start: date, end: date) -> None:
        """Rejeita períodos invertidos antes de qualquer chamada à API."""
        if start > end:
            raise ValueError(f"Período inválido: início {start} é posterior ao fim {end}")