"""Carrega e valida a lista de empresas monitoradas (config/tickers.yaml)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from financial_monitor.config.settings import ConfigurationError

# Aceita tickers da B3 (PETR4.SA) e outros símbolos do Yahoo (^BVSP, BRL=X)
_TICKER_PATTERN = re.compile(r"^[A-Z0-9^][A-Z0-9.=^-]{0,19}$")
_CURRENCY_PATTERN = re.compile(r"^[A-Z]{3}$")


@dataclass(frozen=True)
class Company:
    """Uma empresa monitorada, como definida em tickers.yaml."""

    ticker: str
    name: str
    sector: str | None = None
    currency: str = "BRL"
    active: bool = True


def load_companies(path: Path, *, only_active: bool = True) -> list[Company]:
    """Lê e valida o arquivo de empresas.

    Args:
        path: caminho do tickers.yaml.
        only_active: se True, retorna apenas empresas com active: true.
            Use False para sincronizar a tabela companies, que também
            precisa saber quais empresas foram desativadas.

    Raises:
        ConfigurationError: arquivo ausente, YAML malformado, campos
            inválidos, tickers duplicados ou nenhuma empresa ativa.
    """
    if not path.is_file():
        raise ConfigurationError(f"Arquivo de tickers não encontrado: {path}")

    try:
        content = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigurationError(f"YAML inválido em {path}: {exc}") from exc

    entries = content.get("companies") if isinstance(content, dict) else None
    if not isinstance(entries, list) or not entries:
        raise ConfigurationError(f"{path} deve conter uma lista não vazia na chave 'companies'")

    companies = [_parse_company(entry, position) for position, entry in enumerate(entries, start=1)]
    _ensure_unique(companies)

    if only_active:
        companies = [company for company in companies if company.active]
        if not companies:
            raise ConfigurationError("Nenhuma empresa ativa em tickers.yaml")

    return companies


def _parse_company(entry: Any, position: int) -> Company:
    """Valida um bloco do YAML e o converte em Company."""
    where = f"companies[{position}]"
    if not isinstance(entry, dict):
        raise ConfigurationError(f"{where}: cada empresa deve ser um mapeamento 'chave: valor'")

    raw_ticker = entry.get("ticker")
    if not isinstance(raw_ticker, str):
        raise ConfigurationError(f"{where}: campo 'ticker' é obrigatório e deve ser texto")
    ticker = raw_ticker.strip().upper()
    if not _TICKER_PATTERN.fullmatch(ticker):
        raise ConfigurationError(f"{where}: ticker inválido: {raw_ticker!r}")

    raw_name = entry.get("name")
    if not isinstance(raw_name, str) or not raw_name.strip():
        raise ConfigurationError(f"{where} ({ticker}): campo 'name' é obrigatório")

    raw_sector = entry.get("sector")
    sector = str(raw_sector).strip() if raw_sector is not None else ""

    currency = str(entry.get("currency", "BRL")).strip().upper()
    if not _CURRENCY_PATTERN.fullmatch(currency):
        raise ConfigurationError(f"{where} ({ticker}): 'currency' deve ter 3 letras, ex.: BRL")

    active = entry.get("active", True)
    if not isinstance(active, bool):
        raise ConfigurationError(f"{where} ({ticker}): 'active' deve ser true ou false")

    return Company(
        ticker=ticker,
        name=raw_name.strip(),
        sector=sector or None,
        currency=currency,
        active=active,
    )


def _ensure_unique(companies: list[Company]) -> None:
    """Garante que nenhum ticker aparece duas vezes no arquivo."""
    seen: set[str] = set()
    duplicates: set[str] = set()
    for company in companies:
        if company.ticker in seen:
            duplicates.add(company.ticker)
        seen.add(company.ticker)
    if duplicates:
        raise ConfigurationError(f"Tickers duplicados em tickers.yaml: {sorted(duplicates)}")