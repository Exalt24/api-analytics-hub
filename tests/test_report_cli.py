"""The report command line: argument parsing and the model key switch. No database.

The model is only asked when a key is in the environment and --no-model is absent; the
default of a machine with no key is the template, so these two facts are what make the
command safe to run anywhere.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import monthly_report  # noqa: E402
from app.report.insights import ReportInsightWriter  # noqa: E402


@pytest.mark.parametrize("text, expected", [("2026-10", (2026, 10)), ("2027-01", (2027, 1)), ("2026-12", (2026, 12))])
def test_a_month_is_year_dash_month(text, expected):
    assert monthly_report.parse_month(text) == expected


@pytest.mark.parametrize("text", ["2026-13", "2026-00", "2026-1", "26-10", "october", "2026-10-01", ""])
def test_anything_else_is_refused(text):
    with pytest.raises(argparse.ArgumentTypeError):
        monthly_report.parse_month(text)


def test_no_key_means_no_model(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    assert monthly_report.model_writer(False) is None


def test_a_key_means_a_model_unless_no_model_is_passed(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setenv("LLM_API_KEY", "sentinel-key")
    assert isinstance(monthly_report.model_writer(False), ReportInsightWriter)
    assert monthly_report.model_writer(True) is None
