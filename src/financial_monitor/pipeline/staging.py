"""Área de staging: arquivos Parquet que ligam as etapas do pipeline.

Estrutura:
    data/raw/<run>/prices.parquet           saída do extract
    data/raw/<run>/fundamentals.parquet
    data/processed/<run>/prices.parquet     saída do transform
    data/processed/<run>/fundamentals.parquet
    data/processed/<run>/indicators.parquet

Cada execução tem sua própria pasta, então reexecutar uma etapa sobrescreve
apenas os arquivos daquela execução (idempotência também no staging).
"""

from __future__ import annotations

import logging
import re
import shutil
import time
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

LAYERS = ("raw", "processed")
DEFAULT_RETENTION_DAYS = 30

# O run_id do Airflow contém ":" e "+" (ex.: scheduled__2026-10-23T23:00:00+00:00),
# caracteres proibidos em nomes de pasta no Windows.
_UNSAFE_CHARACTERS = re.compile(r"[^A-Za-z0-9_.-]+")


class StagingError(Exception):
    """Arquivo intermediário ausente ou ilegível. Interrompe o pipeline."""


def run_slug(run_id: str) -> str:
    """Converte o run_id num nome de pasta seguro em qualquer sistema operacional."""
    return _UNSAFE_CHARACTERS.sub("_", run_id).strip("_") or "run"


def run_dir(data_dir: Path, layer: str, run_id: str) -> Path:
    """Pasta de uma execução numa camada (raw ou processed), criada se necessário."""
    if layer not in LAYERS:
        raise ValueError(f"Camada inválida: {layer!r}. Use uma de {LAYERS}")
    path = data_dir / layer / run_slug(run_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_frame(df: pd.DataFrame, path: Path) -> str:
    """Grava um DataFrame em Parquet de forma atômica e retorna o caminho.

    Escreve primeiro num arquivo temporário e depois o renomeia. Se o processo
    morrer no meio da escrita, nunca fica para trás um Parquet pela metade
    com o nome definitivo.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    df.to_parquet(temporary, index=False)
    temporary.replace(path)
    logger.info("Staging: %d registro(s) gravados em %s", len(df), path)
    return str(path)


def read_frame(path: str | Path) -> pd.DataFrame:
    """Lê um Parquet do staging, com mensagem clara se ele não existir."""
    path = Path(path)
    if not path.is_file():
        raise StagingError(
            f"Arquivo de staging não encontrado: {path}. A etapa anterior concluiu "
            "com sucesso e gravou neste mesmo volume?"
        )
    try:
        df = pd.read_parquet(path)
    except Exception as exc:
        raise StagingError(f"Não foi possível ler {path}: {exc}") from exc
    logger.info("Staging: %d registro(s) lidos de %s", len(df), path)
    return df


def purge_old_runs(data_dir: Path, older_than_days: int = DEFAULT_RETENTION_DAYS) -> int:
    """Remove pastas de execuções mais antigas que 'older_than_days'.

    Os dados definitivos estão no PostgreSQL; o staging serve para depuração
    e reprocessamento recente, então não precisa crescer para sempre.
    """
    cutoff = time.time() - older_than_days * 86_400
    removed = 0
    for layer in LAYERS:
        base = data_dir / layer
        if not base.is_dir():
            continue
        for run_path in base.iterdir():
            if run_path.is_dir() and run_path.stat().st_mtime < cutoff:
                try:
                    shutil.rmtree(run_path)
                    removed += 1
                except OSError as exc:
                    logger.warning("Não foi possível remover %s: %s", run_path, exc)
    if removed:
        logger.info("Staging: %d pasta(s) com mais de %d dias removidas", removed, older_than_days)
    return removed