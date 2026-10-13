"""Funções de data e janela de extração, sempre cientes de timezone."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class ExtractionWindow:
    """Período de consulta à API. As duas datas são INCLUSIVAS."""

    start: date
    end: date

    def __post_init__(self) -> None:
        if self.start > self.end:
            raise ValueError(f"Janela inválida: início {self.start} é posterior ao fim {self.end}")

    @property
    def calendar_days(self) -> int:
        """Quantidade de dias corridos na janela, incluindo as duas pontas."""
        return (self.end - self.start).days + 1


def market_today(timezone: str) -> date:
    """Data de hoje no fuso do mercado, não no fuso do servidor."""
    return datetime.now(ZoneInfo(timezone)).date()


def build_extraction_window(reference_date: date, lookback_days: int) -> ExtractionWindow:
    """Monta a janela [reference_date - lookback_days, reference_date].

    A janela é longa (padrão de 400 dias) porque os indicadores de 90 pregões
    precisam de histórico anterior ao período que está sendo atualizado.
    """
    if lookback_days < 1:
        raise ValueError(f"lookback_days deve ser >= 1, recebido: {lookback_days}")
    return ExtractionWindow(
        start=reference_date - timedelta(days=lookback_days),
        end=reference_date,
    )