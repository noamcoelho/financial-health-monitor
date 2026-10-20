"""Indicadores de mercado calculados a partir dos preços limpos.

Fórmulas:
  retorno diário      r_t = P_t / P_(t-1) - 1                 (P = adj_close)
  retorno de 90 dias  R_t = P_t / P_(t-90) - 1                (P = adj_close)
  média móvel         MA_n(t) = média de close nos últimos n pregões
  volatilidade        vol_n(t) = desvio_padrão_amostral(r, n pregões) * raiz(252)

Todas as janelas exigem histórico COMPLETO: sem n pregões, o valor é NaN.
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

TRADING_DAYS_PER_YEAR = 252
MA_WINDOWS: tuple[int, ...] = (7, 30, 90)
VOLATILITY_WINDOWS: tuple[int, ...] = (30, 90)
RETURN_WINDOW = 90

# Maior janela usada. Os primeiros WARMUP_TRADING_DAYS pregões de cada ticker
# não têm histórico suficiente e não devem ser carregados no banco.
WARMUP_TRADING_DAYS = max(*MA_WINDOWS, *VOLATILITY_WINDOWS, RETURN_WINDOW)


def pct_change_safe(current: pd.Series, previous: pd.Series) -> pd.Series:
    """Variação percentual (current / previous - 1) protegida contra bases inválidas.

    Retorna NaN quando 'previous' é nulo, zero ou negativo:
      - zero: divisão impossível;
      - negativo: o percentual perde o sentido (de -10 para +5 seria "-150%").
    'current' pode ser negativo (ex.: lucro que virou prejuízo).
    """
    current = current.astype("float64")
    previous = previous.astype("float64")
    valid = current.notna() & previous.notna() & (previous > 0)

    result = pd.Series(np.nan, index=current.index, dtype="float64")
    result[valid] = current[valid] / previous[valid] - 1
    return result


def daily_returns(adj_close: pd.Series) -> pd.Series:
    """Retorno percentual de um pregão para o seguinte."""
    return pct_change_safe(adj_close, adj_close.shift(1))


def period_return(adj_close: pd.Series, periods: int) -> pd.Series:
    """Retorno acumulado em 'periods' pregões."""
    return pct_change_safe(adj_close, adj_close.shift(periods))


def moving_average(series: pd.Series, window: int) -> pd.Series:
    """Média aritmética simples dos últimos 'window' valores."""
    return series.rolling(window=window, min_periods=window).mean()


def annualized_volatility(returns: pd.Series, window: int) -> pd.Series:
    """Desvio padrão amostral dos retornos na janela, anualizado por raiz(252)."""
    rolling_std = returns.rolling(window=window, min_periods=window).std(ddof=1)
    return rolling_std * np.sqrt(TRADING_DAYS_PER_YEAR)


def compute_price_indicators(prices: pd.DataFrame) -> pd.DataFrame:
    """Calcula os indicadores de mercado, ticker a ticker.

    Espera preços já limpos e ordenados. Retorna uma linha por (ticker, data).
    """
    if prices.empty:
        raise ValueError("Não há preços para calcular indicadores")

    frames: list[pd.DataFrame] = []
    for ticker, group in prices.groupby("ticker", sort=True):
        group = group.sort_values("trade_date")
        returns = daily_returns(group["adj_close"])

        out = group[["ticker", "trade_date"]].copy()
        out["daily_return"] = returns
        out["return_90d"] = period_return(group["adj_close"], RETURN_WINDOW)
        for window in MA_WINDOWS:
            out[f"ma_{window}"] = moving_average(group["close"], window)
        for window in VOLATILITY_WINDOWS:
            out[f"volatility_{window}"] = annualized_volatility(returns, window)

        frames.append(out)
        logger.debug("%s: indicadores calculados para %d pregões", ticker, len(out))

    result = pd.concat(frames, ignore_index=True)
    logger.info("Indicadores de mercado calculados: %d registros", len(result))
    return result


def drop_warmup_rows(indicators: pd.DataFrame, warmup_rows: int = WARMUP_TRADING_DAYS) -> pd.DataFrame:
    """Remove os primeiros 'warmup_rows' pregões de cada ticker.

    Esses pregões não têm histórico suficiente para as janelas longas.
    Carregá-los faria o UPSERT SOBRESCREVER com NaN valores corretos que já
    estão no banco, calculados numa execução anterior com mais histórico.
    """
    position = indicators.groupby("ticker").cumcount()
    kept = indicators[position >= warmup_rows].reset_index(drop=True)

    without_rows = sorted(set(indicators["ticker"]) - set(kept["ticker"]))
    if without_rows:
        logger.warning(
            "Histórico insuficiente (< %d pregões) para gerar indicadores: %s",
            warmup_rows + 1, without_rows,
        )
    logger.info("Período de aquecimento: %d registros descartados", len(indicators) - len(kept))
    return kept