"""Exceções específicas da etapa de carga."""


class LoadError(Exception):
    """Falha ao gravar dados no banco. Sempre interrompe o pipeline."""


class DatabaseConnectionError(LoadError):
    """O PostgreSQL está inacessível (fora do ar, credenciais erradas, rede)."""