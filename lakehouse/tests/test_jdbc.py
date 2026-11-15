"""The main source route: the hub's Postgres tables read over JDBC with the PostgreSQL driver Spark fetches itself.

Needs a Postgres with the pgvector image's extensions (the hub's migrations include pgvector) at LAKEHOUSE_PG_ADMIN, by default the one `docker compose -p lakehouse-pg -f
lakehouse/compose.yaml up -d --wait` starts on 127.0.0.1:5543. Without one the tests skip, unless LAKEHOUSE_REQUIRE_PG is set, as it is in CI, where an unreachable database fails them.
"""
from __future__ import annotations

import os
import socket

import pytest
from pyspark.sql import functions as F

from lakehouse import config
from lakehouse.pipeline import PgSource, RestSource, run_pipeline
from lakehouse.sample_data import SF_ORG_ID, build_world, seed_postgres
from lakehouse.stand_in import StandInSalesforce
from lakehouse.tests.conftest import AS_OF
from lakehouse.warehouse import Lakehouse

ADMIN = os.environ.get("LAKEHOUSE_PG_ADMIN", "postgresql://analytics:analytics@127.0.0.1:5543/postgres")
HOST_PORT = ADMIN.split("@")[1].split("/")[0].split(":")


def reachable() -> bool:
    try:
        with socket.create_connection((HOST_PORT[0], int(HOST_PORT[1])), timeout=2):
            return True
    except OSError:
        return False


@pytest.fixture(scope="module")
def seeded():
    if not reachable():
        if os.environ.get("LAKEHOUSE_REQUIRE_PG"):
            pytest.fail("LAKEHOUSE_REQUIRE_PG is set and Postgres is not reachable at " + ADMIN.split("@")[1])
        pytest.skip("no Postgres at " + ADMIN.split("@")[1])
    world = build_world(seed=11, days=10, end=AS_OF)
    dsn = seed_postgres(world, ADMIN, "lakehouse_test_src")
    host = ADMIN.split("@")[1].rsplit("/", 1)[0]
    return world, f"jdbc:postgresql://{host}/lakehouse_test_src", dsn


def test_the_postgres_tables_land_in_bronze_by_jdbc_and_reach_gold_with_the_same_totals(spark, tmp_path, seeded):
    world, url, dsn = seeded
    lake = Lakehouse(spark, tmp_path / "wh", AS_OF)
    with StandInSalesforce(world.leads[SF_ORG_ID], default_batch=40) as si:
        pg = PgSource("jdbc", url, config.DEFAULT_JDBC_USER, config.DEFAULT_JDBC_PASSWORD)
        rest = RestSource(si.url, "stand-in-token", SF_ORG_ID, batch_size=40, backoff=0.01)
        res = run_pipeline(lake, pg, rest)
        assert res.status == "SUCCEEDED", res.error
        sums = {r["metric_key"]: r["t"] for r in lake.read("silver_daily_figure").groupBy("metric_key").agg(F.sum("value_numeric").alias("t")).collect()}
        expect: dict = {}
        for s in world.snapshots:
            expect[s["metric_key"]] = expect.get(s["metric_key"], 0) + s["value_numeric"]
        assert sums == expect
        assert lake.count("bronze_pg_metric_snapshot") == len(world.snapshots)
        assert "credentials_enc" not in lake.read("bronze_pg_platform_connection").first()["raw_payload"]
        before = {t: lake.version(t) for t in lake.table_names() if t.startswith(("bronze_", "silver_", "gold_"))}
        again = run_pipeline(lake, pg, rest)
        assert again.status == "SUCCEEDED" and {t: lake.version(t) for t in before} == before
        # an incremental read picks up a changed row in Postgres by its synced_at, and only that row
        import psycopg
        with psycopg.connect(dsn) as conn:
            conn.execute("update metric_snapshot set value_numeric = value_numeric + 1, synced_at = synced_at + interval '12 hours' "
                         "where metric_key = 'orders' and observed_on = (select max(observed_on) from metric_snapshot) and connection_id = "
                         "(select id from platform_connection where platform = 'shopify' order by external_account limit 1)")
            conn.commit()
        third = run_pipeline(lake, pg, rest)
        assert third.status in ("SUCCEEDED", "FAILED_QUALITY"), third.error
        steps = {r["step"]: r for r in lake.read(config.OPS_RUN_LOG).where(F.col("run_id") == third.run_id).collect()}
        assert steps["ingest_pg_metric_snapshot"]["rows_written"] == 1 and steps["silver_daily_figure"]["rows_written"] == 1
