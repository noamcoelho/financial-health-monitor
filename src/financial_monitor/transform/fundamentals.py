"""Crescimento trimestral de fundamentos e associação point-in-time aos pregões.

Crescimento (QoQ, trimestre contra trimestre anterior):
    g_t = valor_t / valor_(t-1) - 1
É NaN quando: o trimestre anterior não existe, não é IMEDIATAMENTE anterior
(há lacuna), ou o valor anterior é nulo, zero ou negativo.

Data de disponibilidade (available_from):
    Prazos regulatórios da CVM: ITR (trimestral) em até 45 dias após o fim do
    trimestre; DFP (anual, 4º trimestre) em até 3 meses. Usamos o PRAZO MÁXIMO,
    que é conservador: nunca usamos um balanço antes de ele poder ser público.
    Supõe exercício social terminando em dezembro, como nas empresas monitoradas.
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from financial_monitor.transform.indicators import pct_change_safe

logger = logging.getLogger(__name__)

QUARTERLY_FILING_LAG_DAYS = 45
ANNUAL_FILING_LAG_DAYS = 90

# Um pregão só herda fundamentos publicados há no máximo este número de dias.
# Mais antigo que isso, o dado é considerado desatualizado.
FUNDAMENTALS_STALENESS_DAYS = 200

# Dois trimestres são consecutivos se seus fins distam entre 80 e 100 dias.
_MIN_QUARTER_GAP_DAYS = 80
_MAX_QUARTER_GAP_DAYS = 100

GROWTH_COLUMNS: dict[str, str] = {
    "total_revenue": "revenue_growth_qoq",
    "net_income": "profit_growth_qoq",
    "stockholders_equity": "equity_growth_qoq",
}

FUNDAMENTAL_OUTPUT_COLUMNS: list[str] = [
    "ticker",
    "period_end",
    "available_from",
    "total_revenue",
    "net_income",
    "total_assets",
    "total_liabilities",
    "stockholders_equity",
    *GROWTH_COLUMNS.values(),
]


def compute_available_from(period_end: pd.Series) -> pd.Series:
    """Data conservadora em que o balanço do período se torna público."""
    lag_days = np.where(
        period_end.dt.month == 12, ANNUAL_FILING_LAG_DAYS, QUARTERLY_FILING_LAG_DAYS
    )
    return period_end + pd.to_timedelta(pd.Series(lag_days, index=period_end.index), unit="D")


def compute_fundamental_growth(fundamentals: pd.DataFrame) -> pd.DataFrame:
    """Adiciona available_from e os crescimentos trimestrais."""
    if fundamentals.empty:
        return pd.DataFrame(columns=FUNDAMENTAL_OUTPUT_COLUMNS)

    df = fundamentals.sort_values(["ticker", "period_end"]).reset_index(drop=True)
    df["available_from"] = compute_available_from(df["period_end"])

    by_ticker = df.groupby("ticker")
    gap_days = (df["period_end"] - by_ticker["period_end"].shift(1)).dt.days
    consecutive = gap_days.between(_MIN_QUARTER_GAP_DAYS, _MAX_QUARTER_GAP_DAYS)

    for source, target in GROWTH_COLUMNS.items():
        previous = by_ticker[source].shift(1)
        df[target] = pct_change_safe(df[source], previous).where(consecutive)

    computed = int(df[list(GROWTH_COLUMNS.values())].notna().sum().sum())
    possible = int(consecutive.sum()) * len(GROWTH_COLUMNS)
    logger.info("Crescimentos trimestrais: %d de %d possíveis calculados", computed, possible)
    return df.loc[:, FUNDAMENTAL_OUTPUT_COLUMNS]


def attach_fundamentals(indicators: pd.DataFrame, fundamentals: pd.DataFrame) -> pd.DataFrame:
    """Associa a cada pregão os crescimentos do último balanço JÁ DISPONÍVEL naquela data.

    Usa merge_asof (junção "o mais recente até a data"), que evita look-ahead bias.
    """
    growth_columns = list(GROWTH_COLUMNS.values())

    if fundamentals.empty:
        result = indicators.copy()
        for column in growth_columns:
            result[column] = np.nan
        return result

    left = indicators.copy()
    left["trade_date"] = left["trade_date"].astype("datetime64[ns]")
    right = fundamentals.loc[:, ["ticker", "available_from", *growth_columns]].copy()
    right["available_from"] = right["available_from"].astype("datetime64[ns]")

    merged = pd.merge_asof(
        left.sort_values("trade_date"),
        right.sort_values("available_from"),
        left_on="trade_date",
        right_on="available_from",
        by="ticker",
        direction="backward",
        tolerance=pd.Timedelta(days=FUNDAMENTALS_STALENESS_DAYS),
    )
    return (
        merged.drop(columns="available_from")
        .sort_values(["ticker", "trade_date"])
        .reset_index(drop=True)
    )