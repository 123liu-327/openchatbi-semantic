"""Source adapters perform I/O only when explicitly loaded, never at import."""


class SemanticSourceError(ValueError):
    """Source failures always include a file/table and record location."""
