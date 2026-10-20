"""financial_health_score: indicador EDUCACIONAL de 0 a 100.

AVISO: este score foi criado para fins didáticos neste projeto. Ele NÃO é uma
avaliação financeira profissional e NÃO constitui recomendação de investimento.

Cálculo:
  1. Cada componente é normalizado linearmente para 0-100 entre dois limites
     FIXOS ("pior" recebe 0, "melhor" recebe 100) e cortado nesse intervalo.
  2. O score é a média ponderada dos componentes disponíveis; os pesos dos
     componentes ausentes são redistribuídos proporcionalmente.
  3. Com menos de MIN_COMPONENTS componentes disponíveis, o score é NaN.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
import pandas as pd

from financial_monitor.transform.indicators import pct_change_safe

logger = logging.getLogger(__name__)

TREND_COLUMN = "trend_ma_30_90"
MIN_COMPONENTS = 3


@dataclass(frozen=True)
class ScoreComponent:
    """Um componente do score e sua regra de normalização."""

    column: str
    weight: float
    worst: float  # valor que recebe nota 0
    best: float   # valor que recebe nota 100

    def normalize(self, values: pd.Series) -> pd.Series:
        """Escala linear entre 'worst' e 'best', limitada a [0, 100].

        Funciona também para componentes invertidos (best < worst),
        como a volatilidade, em que menor é melhor.
        """
        scaled = (values.astype("float64") - self.worst) / (self.best - self.worst) * 100
        return scaled.clip(lower=0, upper=100)


SCORE_COMPONENTS: tuple[ScoreComponent, ...] = (
    ScoreComponent("revenue_growth_qoq", weight=0.20, worst=-0.20, best=0.20),
    ScoreComponent("profit_growth_qoq", weight=0.20, worst=-0.50, best=0.50),
    ScoreComponent("return_90d", weight=0.20, worst=-0.30, best=0.30),
    ScoreComponent("volatility_90", weight=0.20, worst=0.60, best=0.15),
    ScoreComponent(TREND_COLUMN, weight=0.20, worst=-0.10, best=0.10),
)


def _check_weights() -> None:
    total = sum(component.weight for component in SCORE_COMPONENTS)
    if not np.isclose(total, 1.0):
        raise ValueError(f"Os pesos do score devem somar 1.0; somam {total}")


_check_weights()


def compute_health_score(indicators: pd.DataFrame) -> pd.DataFrame:
    """Adiciona financial_health_score e score_components_used."""
    result = indicators.copy()
    inputs = result.assign(**{TREND_COLUMN: pct_change_safe(result["ma_30"], result["ma_90"])})

    scores = pd.DataFrame(
        {c.column: c.normalize(inputs[c.column]) for c in SCORE_COMPONENTS},
        index=result.index,
    )
    weights = pd.Series({c.column: c.weight for c in SCORE_COMPONENTS})

    available = scores.notna()
    components_used = available.sum(axis=1)
    available_weight = (available * weights).sum(axis=1)
    weighted_sum = (scores.fillna(0.0) * weights).sum(axis=1)

    score = (weighted_sum / available_weight).where(components_used >= MIN_COMPONENTS)

    result["financial_health_score"] = score.round(2)
    result["score_components_used"] = components_used.astype("int16")

    logger.info(
        "Score calculado para %d de %d registros",
        int(score.notna().sum()), len(result),
    )
    return result