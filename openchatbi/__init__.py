"""OpenChatBI core module initialization.

The global ``config`` object used to be built at import time, which made
``import openchatbi`` pay for the whole configuration and LLM stack. The patrol
job is the clearest victim: its entire premise is that a scheduled run needs no
LLM, yet importing anything from the package pulled the model stack in first.

The loader is built lazily now: importing ``openchatbi`` is cheap and the cost is
paid on first access to ``config``. ``from openchatbi import config`` keeps
working for every existing caller because module-level ``__getattr__`` resolves it
on demand.

One ordering detail is load-bearing: the loader is published to the module global
*before* ``load()`` runs, exactly like the previous
``config = ConfigLoader(); config.load()`` pair. A module imported during
``load()`` that itself does ``from openchatbi import config`` therefore gets the
same half-initialised loader it always did, instead of recursing.
"""

import os
from typing import Any

__all__ = ["config", "Config", "ConfigLoader", "get_default_graph"]

_config_loader = None


def _build_config_loader():
    """Create and load the global configuration exactly once."""
    from openchatbi.config_loader import Config, ConfigLoader

    loader = ConfigLoader()

    # Publish before load(): re-entrant access during load() must see this same
    # loader rather than build a second one.
    global _config_loader
    _config_loader = loader

    if not os.environ.get("SPHINX_BUILD"):
        loader.load()
    else:
        from unittest.mock import MagicMock

        loader._config = Config.from_dict({"default_llm": MagicMock()})
    return loader


def _get_config():
    """Return the process-wide config loader, building it on first use."""
    if _config_loader is None:
        return _build_config_loader()
    return _config_loader


def __getattr__(name: str) -> Any:
    """Resolve the historically eager module attributes on first use."""
    if name == "config":
        return _get_config()
    if name in ("Config", "ConfigLoader"):
        from openchatbi import config_loader

        return getattr(config_loader, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def get_default_graph():
    """
    Build the synchronous mode of the agent graph using default catalog in config.

    Returns:
        CompiledStateGraph: Compiled agent graph ready for execution.
    """
    if os.environ.get("SPHINX_BUILD"):
        return None

    from langgraph.checkpoint.memory import MemorySaver

    from openchatbi.agent_graph import build_agent_graph_sync
    from openchatbi.tool.memory import get_sync_memory_store

    checkpointer = MemorySaver()
    return build_agent_graph_sync(
        _get_config().get().catalog_store,
        checkpointer=checkpointer,
        memory_store=get_sync_memory_store(),
    )
