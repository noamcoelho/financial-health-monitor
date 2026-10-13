"""Configuração central de logging do projeto."""

from __future__ import annotations

import logging
import sys

LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# Bibliotecas que geram mensagens demais em nível DEBUG/INFO
_NOISY_LOGGERS = ("urllib3", "peewee")

_configured = False


def setup_logging(level: str = "INFO") -> None:
    """Configura o logger raiz para escrever no stdout.

    Deve ser chamada UMA vez, no ponto de entrada (ex.: scripts/run_pipeline.py).
    Dentro do Airflow ela NÃO é necessária: o Airflow já captura os logs de
    cada tarefa e os exibe na interface.

    Chamadas repetidas apenas ajustam o nível, sem duplicar handlers.
    """
    global _configured

    root = logging.getLogger()
    root.setLevel(level.upper())

    if _configured:
        return

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT))
    root.addHandler(handler)

    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

    _configured = True