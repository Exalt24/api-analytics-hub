"""Shared fixtures: one Spark session for the whole run, and one fully built warehouse (CSV source, stand-in REST server) that the gold and quality tests copy."""
from __future__ import annotations

import shutil
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from lakehouse import gold, silver  # noqa: E402
from lakehouse.bronze import land_records  # noqa: E402
from lakehouse.pipeline import PgSource, RestSource, run_pipeline  # noqa: E402
from lakehouse.sample_data import TABLE_COLUMNS, rows_of  # noqa: E402
from lakehouse.sample_data import SF_ORG_ID, build_world, export_csv  # noqa: E402
from lakehouse.spark_session import build_spark  # noqa: E402
from lakehouse.stand_in import StandInSalesforce  # noqa: E402
from lakehouse.warehouse import Lakehouse  # noqa: E402

AS_OF = date(2026, 11, 1)


@pytest.fixture(scope="session")
def spark():
    session = build_spark("lakehouse-tests", jdbc=True, shuffle_partitions=2)
    yield session
    session.stop()


@pytest.fixture
def lh(spark, tmp_path):
    lake = Lakehouse(spark, tmp_path / "wh", AS_OF)
    yield lake
    spark.sql(f"DROP DATABASE IF EXISTS {lake.db} CASCADE")


def open_copy(spark, source: Path, dest: Path, as_of: date = AS_OF) -> Lakehouse:
    """A private copy of a built warehouse, so a test can break it."""
    shutil.copytree(source, dest)
    lake = Lakehouse(spark, dest, as_of)
    lake.register_all()
    return lake


@pytest.fixture(scope="session")
def built(spark, tmp_path_factory):
    """Every step run once from the CSV export and the stand-in REST server: the clean state most tests start from."""
    base = tmp_path_factory.mktemp("built")
    world = build_world(seed=7, days=20, end=AS_OF)
    export_csv(world, base / "export")
    with StandInSalesforce(world.leads[SF_ORG_ID], default_batch=40) as si:
        pg = PgSource("csv", export_dir=base / "export")
        rest = RestSource(si.url, "stand-in-token", SF_ORG_ID, batch_size=40, backoff=0.01)
        lake = Lakehouse(spark, base / "wh", AS_OF)
        result = run_pipeline(lake, pg, rest)
        yield SimpleNamespace(path=base / "wh", world=world, result=result, pg=pg, rest=rest, stand_in=si, export=base / "export", lh=lake)


@pytest.fixture(scope="session")
def mini(spark, tmp_path_factory):
    """Silver and gold for six days of the sample world, landed straight into bronze (no REST server, no run log, no quality step): the state the gold and quality tests copy."""
    base = tmp_path_factory.mktemp("mini")
    world = build_world(seed=7, days=6, end=AS_OF)
    lake = Lakehouse(spark, base / "wh", AS_OF)
    for table in TABLE_COLUMNS:
        land_records(lake, f"bronze_pg_{table}", source_name="postgres", source_table=table, run_id="mini-bronze",
                     records=[{k: r[k] for k in TABLE_COLUMNS[table]} for r in rows_of(world, table)])
    land_records(lake, "bronze_sf_lead", source_name="salesforce_rest", source_table="lead", run_id="mini-bronze",
                 records=[(r, SF_ORG_ID) for r in world.leads[SF_ORG_ID]])
    for spec in silver.SPECS.values():
        silver.run_silver(lake, spec, "mini-silver")
    for name in gold.GOLD_ORDER:
        gold.build_gold_table(lake, name)
    return SimpleNamespace(path=base / "wh", world=world, lh=lake)
