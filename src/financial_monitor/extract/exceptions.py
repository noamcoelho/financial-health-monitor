"""Exceções específicas da etapa de extração."""


class ExtractionError(Exception):
    """Erro base da extração. Capturar esta classe captura todas as abaixo."""


class TransientExtractionError(ExtractionError):
    """Falha temporária (rede, timeout, rate limit). Justifica nova tentativa."""


class EmptyDataError(ExtractionError):
    """A API respondeu, mas sem dados (ticker inexistente, deslistado ou sem pregões)."""


class InvalidResponseError(ExtractionError):
    """A API respondeu em formato inesperado (colunas ausentes, estrutura mudou)."""