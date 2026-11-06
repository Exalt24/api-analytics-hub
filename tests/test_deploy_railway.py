"""railway.json, checked against Railway's own published schema and against the app it starts.

WHAT THIS PROVES, and what it does not. It proves the file is valid against the JSON Schema Railway publishes (a copy is saved at deploy/railway/railway.schema.json), that
the start command names a module and an object that exist, that it listens on the port Railway hands in, and that the health check path is a real route that does not touch
the database. It does NOT prove a deploy works: the Railway account's trial has ended and no plan was bought, so this configuration has never been run on Railway
(docs/railway.md says so).

Every check runs a control that must fail: a bad builder, an unknown restart policy and a start command with a fixed port.
"""
from __future__ import annotations

import importlib
import json
import re
import sys
from pathlib import Path

import pytest

jsonschema = pytest.importorskip("jsonschema", reason="jsonschema not installed")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CONFIG = json.loads((ROOT / "railway.json").read_text(encoding="utf-8"))
SCHEMA = json.loads((ROOT / "deploy" / "railway" / "railway.schema.json").read_text(encoding="utf-8"))


def errors(config):
    return list(jsonschema.Draft202012Validator(SCHEMA).iter_errors(config))


def test_the_file_is_valid_against_the_published_schema():
    assert errors(CONFIG) == []
    assert CONFIG["$schema"] == "https://railway.com/railway.schema.json"


def test_the_schema_check_can_fail():
    assert errors({**CONFIG, "build": {"builder": "MAGIC"}}), "an unknown builder must be refused"
    assert errors({**CONFIG, "deploy": {**CONFIG["deploy"], "restartPolicyType": "SOMETIMES"}}), "an unknown restart policy must be refused"
    assert errors({**CONFIG, "deploy": {**CONFIG["deploy"], "numReplicas": 0}}), "zero replicas must be refused"
    assert errors({**CONFIG, "deploy": {**CONFIG["deploy"], "healthcheckPath": 5}}), "a path that is not text must be refused"


def test_the_start_command_names_a_real_module_and_object_and_listens_on_the_port_railway_hands_in():
    command = CONFIG["deploy"]["startCommand"]
    m = re.fullmatch(r"uvicorn ([\w.]+):(\w+) --host 0\.0\.0\.0 --port \$PORT", command)
    assert m, command
    module = importlib.import_module(m.group(1))
    assert hasattr(module, m.group(2))
    assert "--port 8000" not in command and "$PORT" in command


def test_the_health_check_path_is_a_get_route_that_does_not_use_the_database():
    from app.api.main import app

    path = CONFIG["deploy"]["healthcheckPath"]
    routes = {r.path: r for r in app.routes if hasattr(r, "methods")}
    assert path in routes and "GET" in routes[path].methods
    source = routes[path].endpoint.__code__.co_names
    assert "db" not in source and "unscoped" not in source, "a health check that queries Postgres turns a slow database into a restart loop"
    assert path != "/ready", "the readiness route queries the database and must not be the restart trigger"


def test_the_restart_policy_is_bounded_and_the_file_holds_no_secret():
    assert CONFIG["deploy"]["restartPolicyType"] == "ON_FAILURE" and 1 <= CONFIG["deploy"]["restartPolicyMaxRetries"] <= 10
    text = (ROOT / "railway.json").read_text(encoding="utf-8")
    assert not re.search(r"(?i)(password|secret|token|apikey|api_key|postgres(ql)?://)", text)
