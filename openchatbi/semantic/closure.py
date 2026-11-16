"""Deterministic dependency expansion with no configuration, file or service access."""

from collections.abc import Mapping, Sequence

from openchatbi.semantic.models import MetricSpec


class MetricCycleError(ValueError):
    def __init__(self, path: tuple[str, ...]) -> None:
        self.path = path
        super().__init__(f"metric dependency cycle: {' -> '.join(path)}")


class MetricDepthError(ValueError):
    def __init__(self, path: tuple[str, ...], max_depth: int) -> None:
        self.path = path
        self.max_depth = max_depth
        super().__init__(f"metric dependency depth exceeds {max_depth}: {' -> '.join(path)}")


class UnknownMetricError(KeyError):
    def __init__(self, path: tuple[str, ...]) -> None:
        self.path = path
        super().__init__(f"unknown metric {path[-1]!r}; dependency path: {' -> '.join(path)}")


def expand_dependencies(
    metrics: Mapping[str, MetricSpec], names: Sequence[str], *, max_depth: int = 4
) -> tuple[MetricSpec, ...]:
    """Root-first DFS, preserving requested/dependency order; root depth is zero.

    Every path is checked even when a shared dependency already appears in the
    output, so deduplication cannot hide a deeper path that exceeds the limit.
    """
    if max_depth < 0:
        raise ValueError("max_depth must be nonnegative")
    result: list[MetricSpec] = []
    emitted: set[str] = set()

    def visit(name: str, ancestors: tuple[str, ...]) -> None:
        path = (*ancestors, name)
        if name in ancestors:
            raise MetricCycleError(path)
        if len(ancestors) > max_depth:
            raise MetricDepthError(path, max_depth)
        if name not in metrics:
            raise UnknownMetricError(path)
        metric = metrics[name]
        if name not in emitted:
            result.append(metric)
            emitted.add(name)
        for dependency in metric.depends_on:
            visit(dependency, path)

    for name in names:
        visit(name, ())
    return tuple(result)
