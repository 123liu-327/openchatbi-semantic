"""Import downloaded FreshRetailNet Parquet files into an isolated local SQLite database."""

import argparse
import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import yaml

from scripts.import_chatbi_mvp_kb import write_csv

ROOT = Path(__file__).resolve().parents[1]


def import_database(source, output):
    output.mkdir(parents=True, exist_ok=True)
    database = output / "freshretail.sqlite"
    files = {name: source / "data" / f"{name}.parquet" for name in ("train", "eval")}
    hashes = {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in files.items()}
    manifest = output / "import_manifest.json"
    if database.exists():
        if manifest.exists() and json.loads(manifest.read_text(encoding="utf-8"))["source_sha256"] == hashes:
            return database
        raise ValueError("Existing database has a different or missing import manifest")
    temporary = output / "freshretail.importing.sqlite"
    if temporary.exists():
        raise ValueError("Incomplete import exists; inspect freshretail.importing.sqlite before retrying")
    schema = pq.ParquetFile(files["train"]).schema_arrow
    columns = {
        field.name: (
            "TEXT"
            if pa.types.is_list(field.type) or pa.types.is_string(field.type)
            else "INTEGER" if pa.types.is_integer(field.type) else "REAL"
        )
        for field in schema
    }
    definitions = ", ".join(f'"{name}" {kind} NOT NULL' for name, kind in columns.items())
    rows = {}
    with closing(sqlite3.connect(temporary)) as db, db:
        db.execute(f"CREATE TABLE daily_sales ({definitions}, source_split TEXT NOT NULL)")
        placeholders = ",".join("?" for _ in range(len(columns) + 1))
        for split, path in files.items():
            parquet = pq.ParquetFile(path)
            if parquet.schema_arrow != schema:
                raise ValueError("Parquet schemas differ")
            count = 0
            for batch in parquet.iter_batches(batch_size=25000):
                values = []
                for row in batch.to_pylist():
                    values.append(
                        tuple(
                            json.dumps(row[key], separators=(",", ":")) if isinstance(row[key], list) else row[key]
                            for key in columns
                        )
                        + (split,)
                    )
                db.executemany(f"INSERT INTO daily_sales VALUES ({placeholders})", values)
                count += len(values)
                if count % 500000 == 0:
                    print(f"Imported {split}: {count:,}", flush=True)
            if count != parquet.metadata.num_rows:
                raise ValueError("Imported row count mismatch")
            rows[split] = count
            db.commit()
        db.execute("CREATE UNIQUE INDEX daily_sales_grain ON daily_sales(store_id,product_id,dt)")
        db.execute("CREATE INDEX daily_sales_date ON daily_sales(dt)")
        db.execute("CREATE TABLE stores AS SELECT DISTINCT store_id,city_id FROM daily_sales")
        db.execute("CREATE UNIQUE INDEX stores_key ON stores(store_id)")
        db.execute(
            "CREATE TABLE products AS SELECT DISTINCT product_id,management_group_id,first_category_id,second_category_id,third_category_id FROM daily_sales"
        )
        db.execute("CREATE UNIQUE INDEX products_key ON products(product_id)")
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("SQLite integrity check failed")
    temporary.replace(database)
    manifest.write_text(json.dumps({"source_sha256": hashes, "rows": rows}, indent=2), encoding="utf-8")
    return database


def write_catalog(database, output):
    catalog = output / "catalog"
    catalog.mkdir(exist_ok=True)
    descriptions = {
        "daily_sales": "生鲜日销售事实，一行对应门店、商品、日期；sale_amount 是归一化销量。按日期范围查询，来源 train/eval 可合并。",
        "stores": "匿名门店维表，store_id 唯一，关联匿名城市编号。",
        "products": "匿名商品维表，product_id 唯一，含经营组和三级分类。",
    }
    notes = {
        "sale_amount": "归一化日销量，经过统一系数缩放；不能解释为销售金额、人民币或实际件数。",
        "hours_sale": "24小时归一化销量的 JSON 数组。",
        "hours_stock_status": "24小时缺货状态的 JSON 数组。",
        "stock_hour6_22_cnt": "6至22点缺货小时数。",
        "discount": "折扣系数，1为原价，0.9为九折。",
        "dt": "销售日期，格式 YYYY-MM-DD。",
        "source_split": "原始数据分片 train 或 eval，业务统计默认合并。",
        "precpt": "降水量。",
        "avg_temperature": "平均气温。",
        "avg_humidity": "平均湿度。",
        "avg_wind_level": "平均风力。",
    }
    rows = []
    with sqlite3.connect(database) as db:
        for table in descriptions:
            for _, name, kind, *_ in db.execute(f"PRAGMA table_info({table})"):
                rows.append(
                    {
                        "db_name": "",
                        "table_name": table,
                        "column_name": name,
                        "type": kind,
                        "display_name": name,
                        "description": notes.get(name, "匿名编号" if name.endswith("_id") else name),
                        "alias": "",
                        "category": "dimension" if name.endswith("_id") or name in ("dt", "source_split") else "metric",
                    }
                )
    write_csv(catalog / "table_spec_columns.csv", list(rows[0]), rows)
    write_csv(
        catalog / "table_columns.csv",
        ["db_name", "table_name", "column_name"],
        [{key: row[key] for key in ("db_name", "table_name", "column_name")} for row in rows],
    )
    (catalog / "table_info.yaml").write_text(
        yaml.safe_dump(
            {
                "": {
                    name: {
                        "description": description,
                        "selection_rule": "",
                        "sql_rule": "所有销量使用归一化口径；门店和商品使用匿名编号。",
                    }
                    for name, description in descriptions.items()
                }
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    write_csv(
        catalog / "table_selection_example.csv",
        ["question", "selected_tables"],
        [
            {"question": "每日归一化销量趋势", "selected_tables": '["daily_sales"]'},
            {"question": "按城市比较归一化销量", "selected_tables": '["daily_sales","stores"]'},
            {"question": "按商品分类统计归一化销量", "selected_tables": '["daily_sales","products"]'},
        ],
    )
    (catalog / "sql_example.yaml").write_text(
        yaml.safe_dump(
            {
                "": {
                    "daily_sales": "Q: 每日归一化销量\nA: SELECT dt, SUM(sale_amount) AS normalized_sales FROM daily_sales GROUP BY dt ORDER BY dt"
                }
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT / "data/freshretailnet_50k")
    parser.add_argument("--output", type=Path, default=ROOT / "data/freshretail")
    args = parser.parse_args()
    database = import_database(args.source, args.output)
    write_catalog(database, args.output)
    print(f"READY {database}", flush=True)


if __name__ == "__main__":
    main()
