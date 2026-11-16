"""Directed relationship planning; no inferred keys, reverse edges or cross joins."""

import re
from collections import deque
from collections.abc import Sequence

from openchatbi.semantic.models import JoinEdge, JoinPlan


class JoinConflictError(ValueError):
    """An endpoint pair has ambiguous authoritative definitions."""


def merge_edges(*sources: Sequence[JoinEdge]) -> tuple[JoinEdge, ...]:
    """Declared definitions replace FK hints for the same directed endpoint pair.

    Conflicting definitions within the authoritative source are errors, rather
    than arbitrary choices between roles such as owner_id and reviewer_id.
    """
    pairs: dict[tuple[str, str], list[JoinEdge]] = {}
    for source in sources:
        for edge in source:
            pairs.setdefault((edge.child, edge.parent), []).append(edge)
    result = []
    for pair, candidates in sorted(pairs.items()):
        declared = [e for e in candidates if e.source == "declared"]
        authoritative = declared or candidates
        signatures = {
            (tuple(sorted((k.left, k.right) for k in e.keys)), e.join_type, e.relationship) for e in authoritative
        }
        if len(signatures) > 1:
            origins = ", ".join(sorted(e.provenance or e.source for e in authoritative))
            raise JoinConflictError(f"conflicting joins {pair[0]} -> {pair[1]}: {origins}")
        chosen = min(authoritative, key=lambda e: e.model_dump_json())
        if chosen.child == chosen.parent:
            raise JoinConflictError(f"self join requires aliases: {chosen.child}; {chosen.provenance}")
        result.append(chosen)
    return tuple(result)


class JoinGraph:
    def __init__(self, edges: Sequence[JoinEdge], *, tables: Sequence[str] = ()) -> None:
        self._edges = merge_edges(edges)
        self._tables = frozenset(tables) | {t for e in self._edges for t in (e.child, e.parent)}
        adjacency: dict[str, list[JoinEdge]] = {}
        for edge in self._edges:
            adjacency.setdefault(edge.child, []).append(edge)
        self._adjacency = {table: tuple(outgoing) for table, outgoing in adjacency.items()}

    def _tree(self, anchor: str) -> tuple[JoinEdge, ...]:
        """Build an INNER tree first, then expand only LEFT edges.

        A LEFT-then-INNER path cannot meet the linear plan's ordering contract
        without filtering nullable rows. Such paths remain unplanned.
        """
        reached = {anchor}
        tree: list[JoinEdge] = []
        for join_type in ("inner", "left"):
            queue = deque(sorted(reached))
            while queue:
                child = queue.popleft()
                for edge in self._adjacency.get(child, ()):
                    if edge.join_type == join_type and edge.parent not in reached:
                        tree.append(edge)
                        reached.add(edge.parent)
                        queue.append(edge.parent)
        return tuple(tree)

    def select_anchor(self, tables: Sequence[str]) -> str:
        """Maximize reachable requested tables, then avoid fan-out and excess joins."""
        candidates = sorted(set(tables) & self._tables)
        if not candidates:
            return ""

        def score(anchor: str) -> tuple[int, int, int, str]:
            plan = self.plan_join(anchor, tables)
            return (
                len(plan.unreachable),
                sum(e.relationship == "one_to_many" for e in plan.steps),
                len(plan.steps),
                anchor,
            )

        return min(candidates, key=score)

    def plan_join(self, anchor: str, targets: Sequence[str]) -> JoinPlan:
        """Plan only declared directed paths. Empty anchor requests deterministic selection."""
        requested = sorted(set(targets))
        if not anchor:
            selected = self.select_anchor(requested)
            if not selected:
                return JoinPlan(
                    anchor="", steps=(), unreachable=tuple(requested), reason="No known anchor among requested tables"
                )
            plan = self.plan_join(selected, requested)
            return plan.model_copy(
                update={"reason": "Anchor selected by reachability, fan-out, step count, then name. " + plan.reason}
            )
        if anchor not in self._tables:
            return JoinPlan(
                anchor=anchor,
                steps=(),
                unreachable=tuple(sorted(set(requested) | {anchor})),
                reason=f"Unknown anchor: {anchor}",
            )
        tree = self._tree(anchor)
        parents = {edge.parent: edge for edge in tree}
        needed: set[str] = set()
        unreachable = []
        for target in requested:
            if target != anchor and target not in parents:
                unreachable.append(target)
                continue
            cursor = target
            while cursor != anchor:
                needed.add(cursor)
                cursor = parents[cursor].child
        steps = tuple(edge for edge in tree if edge.parent in needed)
        reason = f"Explicit anchor: {anchor}; directed paths with INNER joins before LEFT joins"
        if unreachable:
            reason += "; no compatible directed path to: " + ", ".join(unreachable)
        return JoinPlan(anchor=anchor, steps=steps, unreachable=tuple(unreachable), reason=reason)


def _identifier(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*", value):
        raise ValueError(f"unsupported SQL identifier {value!r}; explicit dialect quoting is required")
    return value


def to_sql_clause(plan: JoinPlan) -> str:
    """Render the reachable part; consumers must inspect unreachable before execution.

    This is a structural SQL fragment, not a dialect-aware query compiler.
    """
    if not plan.anchor or plan.anchor in plan.unreachable:
        if plan.steps:
            raise ValueError("join plan has steps without a reachable anchor")
        return ""
    lines = ["FROM " + _identifier(plan.anchor)]
    reached = {plan.anchor}
    left_started = False
    for edge in plan.steps:
        if edge.child not in reached or edge.parent in reached:
            raise ValueError(f"invalid join order: {edge.child} -> {edge.parent}; {edge.provenance}")
        if left_started and edge.join_type == "inner":
            raise ValueError(f"INNER join follows LEFT join: {edge.child} -> {edge.parent}")
        left_started |= edge.join_type == "left"
        child, parent = _identifier(edge.child), _identifier(edge.parent)
        conditions = [f"{child}.{_identifier(key.left)} = {parent}.{_identifier(key.right)}" for key in edge.keys]
        join_type = "JOIN" if edge.join_type == "inner" else "LEFT JOIN"
        lines.append(f"{join_type} {parent} ON " + "\n    AND ".join(conditions))
        reached.add(edge.parent)
    return "\n".join(lines)
