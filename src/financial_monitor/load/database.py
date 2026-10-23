"""Conexão com o PostgreSQL do projeto via SQLAlchemy."""

from __future__ import annotations

import logging

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, Engine
from sqlalchemy.exc import OperationalError

from financial_monitor.config.settings import Settings
from financial_monitor.load.exceptions import DatabaseConnectionError
from financial_monitor.utils.retry import call_with_retry

logger = logging.getLogger(__name__)

DRIVER = "postgresql+psycopg2"
APPLICATION_NAME = "financial_health_monitor"


def build_database_url(settings: Settings) -> URL:
    """Monta a URL de conexão a partir das variáveis de ambiente.

    URL.create escapa caracteres especiais da senha (@, :, /, #...).
    Montar a URL com f-string quebraria a conexão com senhas como "p@ss:1".
    """
    return URL.create(
        drivername=DRIVER,
        username=settings.postgres_user,
        password=settings.postgres_password,
        host=settings.postgres_host,
        port=settings.postgres_port,
        database=settings.postgres_db,
    )


def create_db_engine(settings: Settings, *, connect_timeout: int = 10) -> Engine:
    """Cria a engine do SQLAlchemy (gerenciador do pool de conexões)."""
    url = build_database_url(settings)
    engine = create_engine(
        url,
        future=True,          # API 2.0 também no SQLAlchemy 1.4
        pool_pre_ping=True,   # testa a conexão antes de usar; descarta as "mortas"
        pool_size=2,
        max_overflow=2,
        connect_args={
            "connect_timeout": connect_timeout,
            "application_name": APPLICATION_NAME,
        },
    )
    logger.info("Engine criada para %s", url.render_as_string(hide_password=True))
    return engine


def verify_connection(
    engine: Engine, *, max_attempts: int = 5, backoff_seconds: float = 2.0
) -> None:
    """Confirma que o banco responde, com retry.

    O retry é útil no Docker: logo após subir o ambiente, o PostgreSQL pode
    levar alguns segundos para aceitar conexões. Com os valores padrão, a
    espera total chega a ~30s (2 + 4 + 8 + 16) antes de desistir.
    """
    safe_url = engine.url.render_as_string(hide_password=True)

    def ping() -> None:
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        except OperationalError as exc:
            raise DatabaseConnectionError(
                f"Não foi possível conectar a {safe_url}: {exc.orig or exc}"
            ) from exc

    call_with_retry(
        ping,
        max_attempts=max_attempts,
        backoff_seconds=backoff_seconds,
        retry_on=(DatabaseConnectionError,),
        description="Conexão com o PostgreSQL",
    )
    logger.info("Conexão com o PostgreSQL verificada")