"""Regras de qualidade de dados (data quality) do pipeline.

Este módulo DECIDE se os dados podem seguir e não altera nada.
As correções ficam em transform/cleaning.py, que reutiliza estas mesmas regras.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from financial_monitor.extract.base import FUNDAMENTAL_COLUMNS, PRICE_COLUMNS

logger = logging.getLogger(__name__)

PRICE_FIELDS: list[str] = ["open", "high", "low", "close", "adj_close"]

# Tolerância para artefatos de ponto flutuante vindos da API (ex.: 24.780001
# contra 24.78). Preços da B3 têm 2 casas decimais, então 0,0001 é seguro.
PRICE_TOLERANCE = 1e-4

# Acima desta fração de registros inválidos, o ticker inteiro é considerado
# suspeito e fica fora da execução.
DEFAULT_MAX_INVALID_RATIO = 0.05


class DataValidationError(Exception):
    """Problema de qualidade grave o suficiente para interromper o pipeline."""


@dataclass
class ValidationReport:
    """Resumo da validação de preços, usado em logs e na auditoria."""

    total_rows: int
    invalid_rows: int
    issues: dict[str, int] = field(default_factory=dict)
    duplicate_keys: int = 0
    rejected_tickers: dict[str, float] = field(default_factory=dict)

    @property
    def is_clean(self) -> bool:
        return self.invalid_rows == 0 and self.duplicate_keys == 0


def check_columns(df: pd.DataFrame, required: list[str], dataset: str) -> None:
    """Interrompe o pipeline se faltar alguma coluna do contrato."""
    missing = [column for column in required if column not in df.columns]
    if missing:
        raise DataValidationError(f"{dataset}: colunas obrigatórias ausentes: {missing}")


def price_issue_masks(
    df: pd.DataFrame, tolerance: float = PRICE_TOLERANCE
) -> dict[str, pd.Series]:
    """Aplica cada regra de qualidade e retorna uma máscara booleana por regra.

    True significa "este registro viola a regra". Comparações com NaN
    resultam em False, por isso valores ausentes têm regras próprias.
    """
    dates = pd.to_datetime(df["trade_date"], errors="coerce")
    numbers = df[[*PRICE_FIELDS, "volume", "dividends", "stock_splits"]].apply(
        pd.to_numeric, errors="coerce"
    )
    prices = numbers[PRICE_FIELDS]
    low, high = numbers["low"], numbers["high"]

    def outside_range(column: str) -> pd.Series:
        return (numbers[column] < low - tolerance) | (numbers[column] > high + tolerance)

    return {
        "data ausente ou inválida": dates.isna(),
        "preço ausente": prices.isna().any(axis=1),
        "preço zero ou negativo": (prices <= 0).any(axis=1),
        "high menor que low": high < low - tolerance,
        "open/close fora do intervalo [low, high]": outside_range("open") | outside_range("close"),
        "volume ausente ou negativo": numbers["volume"].isna() | (numbers["volume"] < 0),
        "volume zero (provável dia sem negociação)": numbers["volume"] == 0,
        "provento ou split negativo": (numbers["dividends"] < 0) | (numbers["stock_splits"] < 0),
    }


def invalid_price_mask(df: pd.DataFrame, tolerance: float = PRICE_TOLERANCE) -> pd.Series:
    """True para registros que violam pelo menos uma regra."""
    combined = pd.Series(False, index=df.index)
    for mask in price_issue_masks(df, tolerance).values():
        combined |= mask
    return combined


def validate_prices(
    df: pd.DataFrame, *, max_invalid_ratio: float = DEFAULT_MAX_INVALID_RATIO
) -> ValidationReport:
    """Valida os preços extraídos e decide quais tickers podem seguir.

    Bloqueia (DataValidationError): colunas ausentes, DataFrame vazio
    ou todos os tickers rejeitados.
    Apenas registra (WARNING): registros inválidos isolados e duplicidades,
    que serão tratados na limpeza.
    """
    check_columns(df, PRICE_COLUMNS, "Preços")
    if df.empty:
        raise DataValidationError("Preços: nenhum registro para validar")

    invalid = pd.Series(False, index=df.index)
    issues: dict[str, int] = {}
    for reason, mask in price_issue_masks(df).items():
        count = int(mask.sum())
        if count:
            issues[reason] = count
            logger.warning("Qualidade: %d registro(s) com %s", count, reason)
        invalid |= mask

    duplicate_keys = int(df.duplicated(subset=["ticker", "trade_date"]).sum())
    if duplicate_keys:
        logger.warning("Qualidade: %d registro(s) duplicados por (ticker, data)", duplicate_keys)

    ratio_by_ticker = invalid.groupby(df["ticker"]).mean()
    rejected = {
        str(ticker): float(ratio)
        for ticker, ratio in ratio_by_ticker.items()
        if ratio > max_invalid_ratio
    }
    for ticker, ratio in rejected.items():
        logger.warning(
            "%s rejeitado: %.1f%% dos registros inválidos (limite: %.1f%%)",
            ticker, ratio * 100, max_invalid_ratio * 100,
        )

    if len(rejected) == df["ticker"].nunique():
        raise DataValidationError("Todos os tickers foram rejeitados pela validação de preços")

    report = ValidationReport(
        total_rows=len(df),
        invalid_rows=int(invalid.sum()),
        issues=issues,
        duplicate_keys=duplicate_keys,
        rejected_tickers=rejected,
    )
    logger.info(
        "Validação de preços: %d registros, %d inválidos, %d duplicados, %d ticker(s) rejeitado(s)",
        report.total_rows, report.invalid_rows, report.duplicate_keys, len(rejected),
    )
    return report


def validate_fundamentals(df: pd.DataFrame) -> None:
    """Fundamentos podem estar ausentes; só o formato é obrigatório."""
    check_columns(df, FUNDAMENTAL_COLUMNS, "Fundamentos")
    if df.empty:
        logger.warning("Fundamentos: nenhum registro. O score usará apenas componentes de mercado.")


def validate_indicators(df: pd.DataFrame) -> None:
    """Confere os indicadores calculados contra as constraints do banco.

    Qualquer violação aqui indica um BUG na transformação (os dados de entrada
    já foram validados), então o pipeline é interrompido antes do Load.
    """
    numeric = df.select_dtypes(include="number")
    checks = {
        "retorno diário <= -100%": df["daily_return"] <= -1,
        "retorno de 90 dias <= -100%": df["return_90d"] <= -1,
        "média móvel não positiva": (df[["ma_7", "ma_30", "ma_90"]] <= 0).any(axis=1),
        "volatilidade negativa": (df[["volatility_30", "volatility_90"]] < 0).any(axis=1),
        "score fora de [0, 100]": (df["financial_health_score"] < 0)
        | (df["financial_health_score"] > 100),
        "valor infinito": np.isinf(numeric).any(axis=1),
        "chave (ticker, data) duplicada": df.duplicated(subset=["ticker", "trade_date"]),
    }
    violations = {rule: int(mask.sum()) for rule, mask in checks.items() if mask.any()}
    if violations:
        raise DataValidationError(f"Indicadores violam regras de qualidade: {violations}")
    logger.info("Validação de indicadores: %d registros sem violações", len(df))