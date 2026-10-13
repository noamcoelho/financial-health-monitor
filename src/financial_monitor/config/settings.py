"""Configuração central do projeto, lida a partir de variáveis de ambiente."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

# src/financial_monitor/config/settings.py -> parents[3] é a raiz do projeto
PROJECT_ROOT = Path(__file__).resolve().parents[3]

_VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


class ConfigurationError(Exception):
    """Configuração ausente ou inválida. Sempre interrompe o pipeline."""


def _get_str(name: str, default: str | None = None) -> str:
    """Lê uma variável de texto. Sem valor e sem default, é obrigatória."""
    value = os.getenv(name, default)
    if value is None or value.strip() == "":
        raise ConfigurationError(f"Variável de ambiente obrigatória não definida: {name}")
    return value.strip()


def _get_int(name: str, default: int, minimum: int = 1) -> int:
    """Lê uma variável inteira, validando o valor mínimo."""
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} deve ser um número inteiro, recebido: {raw!r}") from exc
    if value < minimum:
        raise ConfigurationError(f"{name} deve ser >= {minimum}, recebido: {value}")
    return value


def _get_float(name: str, default: float, minimum: float = 0.0) -> float:
    """Lê uma variável decimal, validando o valor mínimo."""
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} deve ser um número, recebido: {raw!r}") from exc
    if value < minimum:
        raise ConfigurationError(f"{name} deve ser >= {minimum}, recebido: {value}")
    return value


def _get_path(name: str, default_relative: str) -> Path:
    """Lê um caminho. Caminhos relativos são resolvidos a partir da raiz do projeto."""
    raw = os.getenv(name)
    path = Path(raw.strip()) if raw and raw.strip() else Path(default_relative)
    return path if path.is_absolute() else PROJECT_ROOT / path


@dataclass(frozen=True)
class Settings:
    """Configurações validadas do pipeline. Imutável após a criação."""

    postgres_host: str
    postgres_port: int
    postgres_db: str
    postgres_user: str
    postgres_password: str = field(repr=False)  # nunca aparece em logs ou prints
    market_timezone: str = "America/Sao_Paulo"
    extraction_lookback_days: int = 400
    api_timeout_seconds: int = 20
    api_max_retries: int = 3
    api_retry_backoff_seconds: float = 2.0
    api_pause_between_tickers: float = 1.0
    log_level: str = "INFO"
    tickers_file: Path = PROJECT_ROOT / "config" / "tickers.yaml"
    data_dir: Path = PROJECT_ROOT / "data"

    @classmethod
    def from_env(cls) -> Settings:
        """Constrói as configurações a partir das variáveis de ambiente."""
        timezone = _get_str("MARKET_TIMEZONE", "America/Sao_Paulo")
        try:
            ZoneInfo(timezone)
        except ZoneInfoNotFoundError as exc:
            raise ConfigurationError(f"MARKET_TIMEZONE inválido: {timezone!r}") from exc

        log_level = _get_str("LOG_LEVEL", "INFO").upper()
        if log_level not in _VALID_LOG_LEVELS:
            raise ConfigurationError(
                f"LOG_LEVEL deve ser um de {sorted(_VALID_LOG_LEVELS)}, recebido: {log_level!r}"
            )

        return cls(
            postgres_host=_get_str("POSTGRES_HOST", "localhost"),
            postgres_port=_get_int("POSTGRES_PORT", 5432),
            postgres_db=_get_str("POSTGRES_DB"),
            postgres_user=_get_str("POSTGRES_USER"),
            postgres_password=_get_str("POSTGRES_PASSWORD"),
            market_timezone=timezone,
            extraction_lookback_days=_get_int("EXTRACTION_LOOKBACK_DAYS", 400),
            api_timeout_seconds=_get_int("API_TIMEOUT_SECONDS", 20),
            api_max_retries=_get_int("API_MAX_RETRIES", 3),
            api_retry_backoff_seconds=_get_float("API_RETRY_BACKOFF_SECONDS", 2.0),
            api_pause_between_tickers=_get_float("API_PAUSE_BETWEEN_TICKERS", 1.0),
            log_level=log_level,
            tickers_file=_get_path("TICKERS_FILE", "config/tickers.yaml"),
            data_dir=_get_path("DATA_DIR", "data"),
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Retorna as configurações, lendo o .env apenas na primeira chamada.

    override=False: variáveis já definidas no ambiente (por exemplo, pelo
    Docker Compose) têm prioridade sobre o arquivo .env.
    """
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    return Settings.from_env()