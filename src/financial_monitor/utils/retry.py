"""Retry com backoff exponencial para operações sujeitas a falhas temporárias."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


def call_with_retry(
    func: Callable[[], T],
    *,
    max_attempts: int,
    backoff_seconds: float,
    retry_on: tuple[type[BaseException], ...],
    description: str = "operação",
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Executa ``func`` e repete em caso das exceções listadas em ``retry_on``.

    A espera dobra a cada falha (backoff exponencial): com backoff_seconds=2,
    as esperas são 2s, 4s, 8s...

    Exceções fora de ``retry_on`` são propagadas imediatamente: não adianta
    repetir um erro que não é temporário.

    Args:
        func: função sem argumentos a executar.
        max_attempts: número total de tentativas (1 = sem retry).
        backoff_seconds: espera antes da segunda tentativa.
        retry_on: tipos de exceção que justificam nova tentativa.
        description: texto usado nas mensagens de log.
        sleep: função de espera; substituível nos testes para não esperar de verdade.

    Raises:
        A última exceção capturada, quando todas as tentativas falham.
    """
    if max_attempts < 1:
        raise ValueError("max_attempts deve ser >= 1")

    for attempt in range(1, max_attempts + 1):
        try:
            return func()
        except retry_on as exc:
            if attempt == max_attempts:
                logger.error("%s falhou após %d tentativa(s): %s", description, attempt, exc)
                raise
            wait = backoff_seconds * (2 ** (attempt - 1))
            logger.warning(
                "%s falhou (tentativa %d/%d): %s. Nova tentativa em %.1fs.",
                description, attempt, max_attempts, exc, wait,
            )
            sleep(wait)

    raise RuntimeError("Estado inesperado no retry")  # pragma: no cover