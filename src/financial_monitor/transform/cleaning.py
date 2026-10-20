"""Limpeza de preços e fundamentos, com regras documentadas.

Regras de limpeza de PREÇOS (aplicadas nesta ordem):
  1. Remover tickers rejeitados na validação.
  2. Converter tipos: datas sem timezone, números como float.
  3. Dividendos/splits ausentes viram 0 (ausência de evento, não dado faltante).
  4. Arredondar preços para 6 casas (precisão da coluna NUMERIC(18,6)).
  5. Remover duplicidades por (ticker, data), mantendo o último registro.
  6. Corrigir artefatos de ponto flutuante dentro da tolerância.
  7. Remover registros que violam regras de qualidade (NUNCA preencher preços).
  8. Ordenar por ticker e data.

Regras de limpeza de FUNDAMENTOS:
  1. Remover registros sem data de período válida.
  2. Ativos ou passivos negativos (impossíveis) viram NaN, mantendo o restante.
  3. Remover duplicidades por (ticker, período), mantendo o último.
  4. NUNCA interpolar valores ausentes: inventar lucro é pior que não ter.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

import numpy as np
import pandas as pd

from financial_monitor.extract.base import FUNDAMENTAL_COLUMNS, PRICE_COLUMNS
from financial_monitor.validate.quality_checks import (
    PRICE_FIELDS,
    PRICE_TOLERANCE,
    DataValidationError,
    check_columns,
    price_issue_masks,
)

logger = logging.getLogger(__name__)

FUNDAMENTAL_METRICS: list[str] = [
    "total_revenue",
    "net_income",
    "total_assets",
    "total_liabilities",
    "stockholders_equity",
]
_NON_NEGATIVE_METRICS = ("total_assets", "total_liabilities")
_KEY = ["ticker", "trade_date"]


# ----------------------------------------------------------------------
# Preços
# ----------------------------------------------------------------------
def clean_prices(
    raw: pd.DataFrame,
    *,
    rejected_tickers: Iterable[str] = (),
    tolerance: float = PRICE_TOLERANCE,
) -> pd.DataFrame:
    """Aplica as regras de limpeza de preços descritas no topo do módulo."""
    check_columns(raw, PRICE_COLUMNS, "Preços")
    df = raw.loc[:, PRICE_COLUMNS].copy()
    initial_rows = len(df)

    rejected = set(rejected_tickers)
    if rejected:
        before = len(df)
        df = df[~df["ticker"].isin(rejected)]
        logger.warning(
            "Removidos %d registros de tickers rejeitados: %s", before - len(df), sorted(rejected)
        )

    df = _coerce_price_types(df)
    df = _drop_duplicate_days(df)
    df = _fix_float_artifacts(df, tolerance)
    df = _drop_invalid_rows(df)

    df = df.sort_values(_KEY).reset_index(drop=True)
    if df.empty:
        raise DataValidationError("Nenhum registro de preço válido restou após a limpeza")

    df["volume"] = df["volume"].astype("int64")
    logger.info("Limpeza de preços: %d de %d registros mantidos", len(df), initial_rows)
    return df


def _coerce_price_types(df: pd.DataFrame) -> pd.DataFrame:
    df["ticker"] = df["ticker"].astype(str).str.strip().str.upper()

    dates = pd.to_datetime(df["trade_date"], errors="coerce")
    if dates.dt.tz is not None:
        dates = dates.dt.tz_localize(None)
    df["trade_date"] = dates.dt.normalize().astype("datetime64[ns]")

    numeric_columns = [*PRICE_FIELDS, "volume", "dividends", "stock_splits"]
    df[numeric_columns] = df[numeric_columns].apply(pd.to_numeric, errors="coerce")

    # Ausência de dividendo/split significa "não houve evento"
    df[["dividends", "stock_splits"]] = df[["dividends", "stock_splits"]].fillna(0.0)

    df[PRICE_FIELDS] = df[PRICE_FIELDS].round(6)
    return df


def _drop_duplicate_days(df: pd.DataFrame) -> pd.DataFrame:
    exact = df.duplicated(keep="first")
    df = df[~exact]

    conflicting = df.duplicated(subset=_KEY, keep="last")
    df = df[~conflicting]

    if exact.any():
        logger.info("Removidas %d duplicatas idênticas", int(exact.sum()))
    if conflicting.any():
        logger.warning(
            "Removidas %d duplicatas com valores diferentes (mantido o registro mais recente)",
            int(conflicting.sum()),
        )
    return df


def _fix_float_artifacts(df: pd.DataFrame, tolerance: float) -> pd.DataFrame:
    """Ajusta desvios minúsculos causados por ponto flutuante.

    Ex.: close = 24.780001 e high = 24.78 representam o mesmo preço, mas
    violariam a constraint "close BETWEEN low AND high" do banco. Só ajustamos
    desvios menores que a tolerância; desvios maiores são erros reais e serão
    removidos na etapa seguinte.
    """
    adjustments = 0

    tiny_inversion = (df["high"] < df["low"]) & (df["low"] - df["high"] <= tolerance)
    df.loc[tiny_inversion, "high"] = df.loc[tiny_inversion, "low"]
    adjustments += int(tiny_inversion.sum())

    for column in ("open", "close"):
        below = (df[column] < df["low"]) & (df["low"] - df[column] <= tolerance)
        above = (df[column] > df["high"]) & (df[column] - df["high"] <= tolerance)
        df.loc[below, column] = df.loc[below, "low"]
        df.loc[above, column] = df.loc[above, "high"]
        adjustments += int(below.sum() + above.sum())

    if adjustments:
        logger.info("%d ajuste(s) de arredondamento aplicados (desvio <= %g)", adjustments, tolerance)
    return df


def _drop_invalid_rows(df: pd.DataFrame) -> pd.DataFrame:
    """Remove registros inválidos. Tolerância zero: o que sobra respeita o banco."""
    invalid = pd.Series(False, index=df.index)
    for reason, mask in price_issue_masks(df, tolerance=0.0).items():
        if mask.any():
            tickers = sorted(df.loc[mask, "ticker"].unique())
            logger.warning("Removidos %d registro(s) com %s: %s", int(mask.sum()), reason, tickers)
        invalid |= mask
    return df[~invalid]


# ----------------------------------------------------------------------
# Fundamentos
# ----------------------------------------------------------------------
def clean_fundamentals(
    raw: pd.DataFrame, *, rejected_tickers: Iterable[str] = ()
) -> pd.DataFrame:
    """Aplica as regras de limpeza de fundamentos descritas no topo do módulo."""
    check_columns(raw, FUNDAMENTAL_COLUMNS, "Fundamentos")
    if raw.empty:
        return pd.DataFrame(columns=FUNDAMENTAL_COLUMNS)

    df = raw.loc[:, FUNDAMENTAL_COLUMNS].copy()
    df = df[~df["ticker"].isin(set(rejected_tickers))]
    df["ticker"] = df["ticker"].astype(str).str.strip().str.upper()
    df["period_end"] = (
        pd.to_datetime(df["period_end"], errors="coerce").dt.normalize().astype("datetime64[ns]")
    )

    invalid_dates = df["period_end"].isna()
    if invalid_dates.any():
        logger.warning("Removidos %d trimestre(s) sem data válida", int(invalid_dates.sum()))
        df = df[~invalid_dates]

    df[FUNDAMENTAL_METRICS] = df[FUNDAMENTAL_METRICS].apply(pd.to_numeric, errors="coerce")

    for column in _NON_NEGATIVE_METRICS:
        negative = df[column] < 0
        if negative.any():
            logger.warning(
                "%d valor(es) negativo(s) em %s tratados como ausentes: %s",
                int(negative.sum()), column, sorted(df.loc[negative, "ticker"].unique()),
            )
            df.loc[negative, column] = np.nan

    df = df.drop_duplicates(subset=["ticker", "period_end"], keep="last")
    df = df.dropna(subset=FUNDAMENTAL_METRICS, how="all")
    df[FUNDAMENTAL_METRICS] = df[FUNDAMENTAL_METRICS].round(2)

    logger.info("Limpeza de fundamentos: %d trimestre(s) mantidos", len(df))
    return df.sort_values(["ticker", "period_end"]).reset_index(drop=True)