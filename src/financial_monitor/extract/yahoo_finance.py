"""Fonte de dados Yahoo Finance, via biblioteca yfinance (não oficial)."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from datetime import date, timedelta
from typing import TypeVar

import pandas as pd
import yfinance as yf

from financial_monitor.config.settings import Settings
from financial_monitor.extract.base import (
    FUNDAMENTAL_COLUMNS,
    PRICE_COLUMNS,
    FinancialDataSource,
)
from financial_monitor.extract.exceptions import (
    EmptyDataError,
    ExtractionError,
    InvalidResponseError,
    TransientExtractionError,
)
from financial_monitor.utils.retry import call_with_retry

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Colunas obrigatórias da resposta do yfinance -> nomes do nosso contrato
_PRICE_SOURCE_COLUMNS = {
    "Open": "open",
    "High": "high",
    "Low": "low",
    "Close": "close",
    "Adj Close": "adj_close",
    "Volume": "volume",
}
# Colunas de eventos corporativos: ausência significa "nenhum evento"
_ACTION_COLUMNS = {"Dividends": "dividends", "Stock Splits": "stock_splits"}

# Linhas das demonstrações, em ordem de preferência. Empresas de setores
# diferentes (ex.: bancos) nem sempre usam o mesmo rótulo.
_INCOME_LINES: dict[str, tuple[str, ...]] = {
    "total_revenue": ("Total Revenue", "Operating Revenue"),
    "net_income": ("Net Income", "Net Income Common Stockholders"),
}
_BALANCE_LINES: dict[str, tuple[str, ...]] = {
    "total_assets": ("Total Assets",),
    "total_liabilities": ("Total Liabilities Net Minority Interest", "Total Liabilities"),
    "stockholders_equity": (
        "Stockholders Equity",
        "Common Stock Equity",
        "Total Equity Gross Minority Interest",
    ),
}


def _transient_error_types() -> tuple[type[BaseException], ...]:
    """Monta a lista de exceções consideradas temporárias.

    O yfinance já usou bibliotecas HTTP diferentes ao longo das versões
    (requests, curl_cffi). Importamos cada uma de forma opcional para que
    o código funcione independentemente da versão instalada.
    """
    errors: list[type[BaseException]] = [TimeoutError, ConnectionError]
    try:
        from yfinance.exceptions import YFRateLimitError

        errors.append(YFRateLimitError)
    except ImportError:
        pass
    try:
        from curl_cffi.requests.exceptions import RequestException as CurlRequestException

        errors.append(CurlRequestException)
    except ImportError:
        pass
    try:
        from requests.exceptions import RequestException

        errors.append(RequestException)
    except ImportError:
        pass
    return tuple(errors)


_TRANSIENT_ERRORS = _transient_error_types()


class YahooFinanceSource(FinancialDataSource):
    """Extrai preços e fundamentos do Yahoo Finance."""

    name = "yahoo_finance"

    def __init__(
        self,
        timeout_seconds: int = 20,
        max_retries: int = 3,
        backoff_seconds: float = 2.0,
    ) -> None:
        self._timeout = timeout_seconds
        self._max_retries = max_retries
        self._backoff = backoff_seconds

    @classmethod
    def from_settings(cls, settings: Settings) -> YahooFinanceSource:
        """Cria a fonte usando os parâmetros definidos no .env."""
        return cls(
            timeout_seconds=settings.api_timeout_seconds,
            max_retries=settings.api_max_retries,
            backoff_seconds=settings.api_retry_backoff_seconds,
        )

    # ------------------------------------------------------------------
    # Preços
    # ------------------------------------------------------------------
    def get_prices(self, ticker: str, start: date, end: date) -> pd.DataFrame:
        self.validate_period(start, end)
        logger.info("Extraindo preços de %s (%s a %s)", ticker, start, end)

        raw = self._call_api(
            lambda: yf.Ticker(ticker).history(
                start=start.isoformat(),
                # No yfinance o 'end' é EXCLUSIVO; somamos 1 dia para incluir 'end'
                end=(end + timedelta(days=1)).isoformat(),
                interval="1d",
                auto_adjust=False,  # mantém Close e Adj Close separados
                actions=True,       # inclui dividendos e splits
                timeout=self._timeout,
            ),
            description=f"Preços de {ticker}",
        )

        if not isinstance(raw, pd.DataFrame) or raw.empty:
            raise EmptyDataError(
                f"{ticker}: nenhum pregão retornado entre {start} e {end} "
                "(ticker inexistente, deslistado ou período sem negociação)"
            )

        prices = _standardize_prices(raw, ticker)
        logger.info("%s: %d registros de preço encontrados", ticker, len(prices))
        return prices

    # ------------------------------------------------------------------
    # Fundamentos
    # ------------------------------------------------------------------
    def get_fundamentals(self, ticker: str) -> pd.DataFrame:
        logger.info("Extraindo fundamentos de %s", ticker)

        income, balance = self._call_api(
            lambda: _download_statements(ticker),
            description=f"Fundamentos de {ticker}",
        )

        lines = {column: _pick_line(income, labels) for column, labels in _INCOME_LINES.items()}
        lines.update(
            {column: _pick_line(balance, labels) for column, labels in _BALANCE_LINES.items()}
        )

        # Trimestres em que TODAS as linhas estão vazias não carregam informação
        fundamentals = pd.DataFrame(lines).dropna(how="all")
        if fundamentals.empty:
            logger.warning("%s: nenhum dado fundamentalista disponível", ticker)
            return pd.DataFrame(columns=FUNDAMENTAL_COLUMNS)

        fundamentals.index = pd.to_datetime(fundamentals.index).normalize()
        fundamentals = (
            fundamentals.rename_axis("period_end")
            .reset_index()
            .sort_values("period_end")
        )
        fundamentals.insert(0, "ticker", ticker)

        logger.info("%s: %d trimestres de fundamentos encontrados", ticker, len(fundamentals))
        return fundamentals.loc[:, FUNDAMENTAL_COLUMNS].reset_index(drop=True)

    # ------------------------------------------------------------------
    # Infraestrutura de chamada
    # ------------------------------------------------------------------
    def _call_api(self, func: Callable[[], T], *, description: str) -> T:
        """Executa uma chamada ao yfinance, traduzindo erros e aplicando retry."""

        def attempt() -> T:
            try:
                return func()
            except _TRANSIENT_ERRORS as exc:
                raise TransientExtractionError(
                    f"{description}: falha temporária ({type(exc).__name__}: {exc})"
                ) from exc
            except ExtractionError:
                raise
            except Exception as exc:
                # Erro inesperado dentro da biblioteca: provavelmente o Yahoo
                # mudou o formato da resposta. Repetir não resolve.
                raise InvalidResponseError(
                    f"{description}: erro inesperado da biblioteca ({type(exc).__name__}: {exc})"
                ) from exc

        return call_with_retry(
            attempt,
            max_attempts=self._max_retries,
            backoff_seconds=self._backoff,
            retry_on=(TransientExtractionError,),
            description=description,
        )


# ----------------------------------------------------------------------
# Funções auxiliares (sem estado, fáceis de testar isoladamente)
# ----------------------------------------------------------------------
def _standardize_prices(raw: pd.DataFrame, ticker: str) -> pd.DataFrame:
    """Converte a resposta do yfinance para o formato PRICE_COLUMNS."""
    missing = set(_PRICE_SOURCE_COLUMNS) - set(raw.columns)
    if missing:
        raise InvalidResponseError(f"{ticker}: colunas ausentes na resposta: {sorted(missing)}")

    df = raw.rename(columns={**_PRICE_SOURCE_COLUMNS, **_ACTION_COLUMNS})

    for column in _ACTION_COLUMNS.values():
        if column not in df.columns:
            df[column] = 0.0

    numeric_columns = [c for c in PRICE_COLUMNS if c not in ("ticker", "trade_date")]
    # Valores não numéricos viram NaN aqui e são tratados na validação/limpeza
    df[numeric_columns] = df[numeric_columns].apply(pd.to_numeric, errors="coerce")

    index = pd.DatetimeIndex(df.index)
    if index.tz is not None:
        # Remove o fuso mantendo a data local da bolsa (America/Sao_Paulo para .SA)
        index = index.tz_localize(None)
    df["trade_date"] = index.normalize()
    df["ticker"] = ticker

    return df.loc[:, PRICE_COLUMNS].reset_index(drop=True)


def _download_statements(ticker: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Baixa DRE e balanço trimestrais. Retorna DataFrames vazios se ausentes."""
    yf_ticker = yf.Ticker(ticker)
    income = yf_ticker.quarterly_income_stmt
    balance = yf_ticker.quarterly_balance_sheet
    return (
        income if isinstance(income, pd.DataFrame) else pd.DataFrame(),
        balance if isinstance(balance, pd.DataFrame) else pd.DataFrame(),
    )


def _pick_line(statement: pd.DataFrame, labels: Iterable[str]) -> pd.Series:
    """Retorna a primeira linha encontrada entre os rótulos candidatos."""
    for label in labels:
        if label in statement.index:
            line = statement.loc[label]
            if isinstance(line, pd.DataFrame):  # rótulo duplicado na resposta
                line = line.iloc[0]
            logger.debug("Usando a linha '%s' da demonstração", label)
            return pd.to_numeric(line, errors="coerce")
    return pd.Series(dtype="float64")