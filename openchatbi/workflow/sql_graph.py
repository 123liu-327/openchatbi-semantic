"""SQL compilation from semantic-domain field selections, with independently evaluable control nodes."""

import json
import time

from langgraph.graph import END, START, StateGraph

from openchatbi.constants import SQL_SUCCESS
from openchatbi.graph_state import PreparedSQLInputState, SQLGraphState, SQLOutputState
from openchatbi.semantic.join import JoinGraph, to_sql_clause
from openchatbi.semantic.render import render_semantic_block
from openchatbi.workflow.ledger import RunLedger
from openchatbi.workflow.models import ResolvedQuery, SelectedTable
from openchatbi.workflow.security import check_resolved_sql, check_sql, require_authorization, sql_resources


def build_sql_generation_graph(catalog, cfg, llm, create_nodes, *, checkpointer=None, store=None):
    generate, execute, regenerate, visualize, score, confidence = create_nodes(
        llm,
        catalog,
        dialect=cfg.dialect,
        visualization_mode=cfg.visualization_mode,
    )

    def resolved(state):
        return ResolvedQuery.model_validate(state["resolved_query"])

    def relationship_graph(query):
        return JoinGraph(query.relationships, tables=[row.table for row in [*query.tables, *query.join_tables]])

    def select_anchor(state):
        query = resolved(state)
        targets = [row["table"] for row in state["tables"]]
        graph = relationship_graph(query)
        anchor = graph.select_anchor(targets)
        if query.anchor_hint in targets and not graph.plan_join(query.anchor_hint, targets).unreachable:
            anchor = query.anchor_hint
        if not anchor:
            raise ValueError("No main table can be selected from the semantic contract")
        return {"anchor": anchor}

    def plan_joins(state):
        query = resolved(state)
        plan = relationship_graph(query).plan_join(state["anchor"], [row["table"] for row in state["tables"]])
        if plan.unreachable:
            raise ValueError("Confirmed tables have no compatible JOIN path: " + ", ".join(plan.unreachable))
        names = {row["table"] for row in state["tables"]} | {plan.anchor}
        names.update(table for edge in plan.steps for table in (edge.child, edge.parent))
        available = {row.table: row for row in [*query.tables, *query.join_tables]}
        if names - set(available):
            raise ValueError("JOIN requires a table not supplied by the semantic layer")
        selected = {row["table"]: row for row in state["tables"]}
        rows = [selected.get(name, available[name].model_dump()) for name in sorted(names)]
        columns = {row["table"]: set(row["columns"]) for row in rows}
        for edge in plan.steps:
            for key in edge.keys:
                if key.left not in columns[edge.child] or key.right not in columns[edge.parent]:
                    raise ValueError("JOIN key is outside the confirmed semantic fields")
        tables = [
            {
                **query.table_metadata.get(name, {"table": name}),
                "columns": [
                    col
                    for col in query.table_metadata.get(name, {}).get("columns", [])
                    if col["column_name"] in columns[name]
                ],
            }
            for name in sorted(names)
        ]
        prompt = json.dumps(tables, ensure_ascii=False, indent=2) + "\n" + query.semantic_block
        prompt += "\n" + render_semantic_block(edges=plan.steps) + "\nDeclared JOIN skeleton:\n" + to_sql_clause(plan)
        return {"tables": rows, "join_plan": plan.model_dump(mode="json"), "prepared_schema": prompt}

    def review_sql(state):
        query = resolved(state).model_copy(
            update={"tables": [SelectedTable.model_validate(row) for row in state["tables"]]}
        )
        check_sql(state.get("sql", ""), cfg.dialect)
        check_resolved_sql(state["sql"], query, cfg.dialect)
        resources = sql_resources(state["sql"], query, cfg.dialect)
        if not {table.casefold() for table in resolved(state).required_tables} <= resources.keys():
            raise ValueError("Generated SQL omits confirmed semantic tables")
        review_join_keys(state["sql"], query, state["join_plan"], cfg.dialect)
        return {}

    def authorize_user(state):
        return {"authorization": require_authorization(state["sql"], cfg.dialect)}

    def stage(name, function):
        def run(state):
            started = time.monotonic()
            try:
                output = function(state)
                output["stage_error"] = ""
                status = (
                    "error" if name == "execute_sql" and output.get("sql_execution_result") != SQL_SUCCESS else "ok"
                )
            except Exception as exc:
                from langgraph.errors import GraphInterrupt

                if isinstance(exc, GraphInterrupt):
                    raise
                message = (
                    str(exc)
                    if isinstance(exc, (ValueError, PermissionError))
                    else f"{name} failed ({type(exc).__name__})"
                )
                status = "blocked" if isinstance(exc, PermissionError) else "error"
                output = {"stage_error": message}
                if name == "review_sql":
                    output["previous_sql_errors"] = [
                        *state.get("previous_sql_errors", []),
                        {
                            "sql": state.get("sql", ""),
                            "error": message,
                            "error_type": "SQLReviewError",
                        },
                    ]
            record = RunLedger(cfg.report_directory, state["request_id"]).record(
                task_id=state["task_id"],
                domain="text2sql",
                node=name,
                status=status,
                inputs={
                    key: state.get(key) for key in ("resolved_query", "anchor", "join_plan", "sql", "sql_retry_count")
                },
                outputs={key: value for key, value in output.items() if key != "messages"},
                duration_ms=(time.monotonic() - started) * 1000,
            )
            return {**output, "sql_stages": [record]}

        return run

    graph = StateGraph(SQLGraphState, input_schema=PreparedSQLInputState, output_schema=SQLOutputState)
    nodes = {
        "select_anchor": select_anchor,
        "plan_joins": plan_joins,
        "generate_sql": generate,
        "review_sql": review_sql,
        "authorize_user": authorize_user,
        "execute_sql": execute,
        "regenerate_sql": regenerate,
        "score_sql": score,
        "confidence_gate": confidence,
        "generate_visualization": visualize,
    }
    for name, function in nodes.items():
        graph.add_node(name, stage(name, function))
    graph.add_edge(START, "select_anchor")
    for current, following in (
        ("select_anchor", "plan_joins"),
        ("plan_joins", "generate_sql"),
        ("generate_sql", "review_sql"),
        ("regenerate_sql", "review_sql"),
        ("authorize_user", "execute_sql"),
        ("score_sql", "confidence_gate"),
    ):
        graph.add_conditional_edges(
            current, lambda state, target=following: END if state.get("stage_error") else target
        )

    def after_review(state):
        if not state.get("stage_error"):
            return "authorize_user"
        return "regenerate_sql" if state.get("sql_retry_count", 0) < cfg.sql_max_retries else END

    def after_execute(state):
        if state.get("stage_error"):
            return END
        from openchatbi.text2sql.sql_graph import _route_after_execute

        return {"success": "score_sql", "regenerate_sql": "regenerate_sql", "end": END}[_route_after_execute(state)]

    def after_confidence(state):
        if state.get("stage_error"):
            return END
        from openchatbi.text2sql.sql_graph import route_after_confidence

        target = route_after_confidence(state)
        # Edited SQL must pass safety review and authorization again.
        return {"end": END, "execute_sql": "review_sql"}.get(target, target)

    graph.add_conditional_edges("review_sql", after_review)
    graph.add_conditional_edges("execute_sql", after_execute)
    graph.add_conditional_edges("confidence_gate", after_confidence)
    graph.add_edge("generate_visualization", END)
    return graph.compile(name="sql_generation", checkpointer=checkpointer, store=store)


def review_join_keys(sql, query, plan, dialect):
    """Reject cartesian joins and invented physical join keys; retain CTE support."""
    import sqlglot
    from sqlglot import exp
    from sqlglot.optimizer.qualify import qualify
    from sqlglot.optimizer.scope import traverse_scope

    read = {"postgresql": "postgres"}.get(dialect, dialect)
    tree = qualify(
        sqlglot.parse_one(sql, read=read),
        dialect=read,
        schema={row.table: dict.fromkeys(row.columns, "UNKNOWN") for row in query.tables},
    )
    for scope in traverse_scope(tree):
        for join in scope.expression.args.get("joins", []):
            if not join.args.get("on") and not join.args.get("using"):
                raise ValueError("Cartesian JOIN is not allowed")
            pairs = set()
            for equality in (join.args.get("on") or exp.Null()).find_all(exp.EQ):
                if not isinstance(equality.left, exp.Column) or not isinstance(equality.right, exp.Column):
                    continue
                values = []
                for column in (equality.left, equality.right):
                    source = scope.sources.get(column.table)
                    if isinstance(source, exp.Table):
                        values.append((source.name.casefold(), column.name.casefold()))
                if len(values) == 2:
                    pairs.add(frozenset(values))
            source = scope.sources.get(join.this.alias_or_name)
            if isinstance(source, exp.Table):
                edges = [edge for edge in plan["steps"] if source.name in (edge["child"], edge["parent"])]
                if not any(
                    all(
                        frozenset(
                            (
                                (edge["child"].casefold(), key["left"].casefold()),
                                (edge["parent"].casefold(), key["right"].casefold()),
                            )
                        )
                        in pairs
                        for key in edge["keys"]
                    )
                    for edge in edges
                ):
                    raise ValueError("SQL JOIN keys do not match the deterministic relationship plan")
