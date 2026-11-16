from pathlib import Path

import pytest

from openchatbi.catalog.store.file_system import FileSystemCatalogStore
from openchatbi.semantic.models import BusinessRule
from openchatbi.semantic.rules import RuleConflictError, apply_rules, match_rules
from scripts.validate_semantic_data import read_yaml

DATA = Path(__file__).resolve().parents[2] / "example/semantic"


@pytest.fixture
def catalog():
    return FileSystemCatalogStore(str(DATA / "catalog"), {"uri": "sqlite:///:memory:"})


@pytest.mark.parametrize(
    "question",
    [
        "收入",
        "销售额",
        "营业收入",
        "营收",
        "今年收入",
        "月度销售额",
        "按产品收入",
        "按区域收入",
        "成本",
        "销售成本",
        "毛利",
        "利润",
        "毛利润",
        "产品成本",
        "季度成本",
        "年度利润",
        "客户收入",
        "订单收入",
        "各区毛利",
        "本月营收",
    ],
)
def test_twenty_questions_never_include_forbidden_columns(question, catalog):
    rules = tuple(BusinessRule.model_validate(row) for row in read_yaml(DATA / "rules.yaml")["rules"])
    active = match_rules(rules, question)
    selected = [{"table": "sales_orders", "columns": []}]
    result = apply_rules(selected, active, catalog)
    columns = {f"{row['table']}.{col}" for row in result for col in row["columns"]}
    forbidden = {col for rule in active for col in rule.forbid_columns}
    assert not columns & forbidden
    assert selected == [{"table": "sales_orders", "columns": []}]
    if any(word in question for word in ("成本", "毛利", "利润")):
        assert "dim_products.material_cost" in columns
        assert "dim_products.labor_cost" in columns
        assert "dim_products.standard_cost" not in columns


def test_tax_exception_is_data_driven(catalog):
    rules = tuple(BusinessRule.model_validate(row) for row in read_yaml(DATA / "rules.yaml")["rules"])
    result = apply_rules([{"table": "sales_orders"}], match_rules(rules, "含税收入"), catalog)
    assert "gross_amount" in result[0]["columns"]
    assert "net_amount" not in result[0]["columns"]


def test_unresolved_conflict_is_not_silently_overridden(catalog):
    rules = (
        BusinessRule(name="first", trigger_any=("x",), require_columns=("net_amount",)),
        BusinessRule(name="second", trigger_any=("x",), forbid_columns=("net_amount",)),
    )
    with pytest.raises(RuleConflictError, match="overlap"):
        apply_rules([{"table": "sales_orders"}], rules, catalog)


def test_empty_and_scoped_rules(catalog):
    assert match_rules((), "anything") == ()
    rule = BusinessRule(name="scoped", trigger_any=("TOTAL",), scope_tables=("chosen",), exclude_any=("skip",))
    assert match_rules((rule,), "total", ["other"]) == ()
    assert match_rules((rule,), "skip total", ["chosen"]) == ()
    assert match_rules((rule,), "total", ["chosen"]) == (rule,)
