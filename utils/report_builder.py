"""Utility helpers to build and export daily observability reports."""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable

import config
from utils import metrics
import utils.cache as cache_module
from utils.daily_risk import (
    get_today_pnl,
    PNL_LOG_FILE,
    EQUITY_LOG_FILE,
)

DEFAULT_FUNNEL_FIELDS = [
    "scanned",
    "gated",
    "scored",
    "approved",
    "ordered",
    "rejected",
]


def _reporting_cfg(policy: Dict[str, Any] | None = None) -> Dict[str, Any]:
    policy = policy or getattr(config, "_policy", {}) or {}
    return (policy.get("reporting") or {}).copy()


def _fmt_currency(value: Any) -> str:
    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return "$0.00"


def _fmt_signed_currency(value: Any) -> str:
    try:
        amount = float(value)
    except (TypeError, ValueError):
        amount = 0.0
    sign = "+" if amount >= 0 else "-"
    return f"{sign}${abs(amount):,.2f}"


def _get_equity_snapshot() -> float:
    if not EQUITY_LOG_FILE.exists():
        return 0.0
    try:
        with open(EQUITY_LOG_FILE, newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            last_value = 0.0
            for row in reader:
                try:
                    last_value = float(row.get("equity", 0) or 0)
                except (TypeError, ValueError):
                    continue
            return last_value
    except Exception:
        return 0.0


def _get_cumulative_pnl() -> float:
    if not PNL_LOG_FILE.exists():
        return 0.0
    total = 0.0
    try:
        with open(PNL_LOG_FILE, newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                try:
                    total += float(row.get("pnl_usd", 0) or 0)
                except (TypeError, ValueError):
                    continue
    except Exception:
        return 0.0
    return total


def _get_daily_drawdown_pct(equity: float) -> float:
    if equity <= 0 or not PNL_LOG_FILE.exists():
        return 0.0
    today = datetime.utcnow().date().isoformat()
    cumulative = 0.0
    worst = 0.0
    try:
        with open(PNL_LOG_FILE, newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                if row.get("date") != today:
                    continue
                try:
                    pnl = float(row.get("pnl_usd", 0) or 0)
                except (TypeError, ValueError):
                    continue
                cumulative += pnl
                if cumulative < worst:
                    worst = cumulative
    except Exception:
        return 0.0
    if worst >= 0:
        return 0.0
    return round((worst / equity) * 100.0, 4)


def _get_market_exposure(policy: Dict[str, Any]) -> float:
    try:
        from core.executor import get_market_exposure_factor

        return float(get_market_exposure_factor(policy))
    except Exception:
        market_cfg = (policy or {}).get("market", {})
        try:
            return float(market_cfg.get("default_exposure", 1.0))
        except (TypeError, ValueError):
            return 1.0


def _collect_risk_metrics(policy: Dict[str, Any]) -> Dict[str, float]:
    equity = _get_equity_snapshot()
    daily_pnl = get_today_pnl()
    cumulative_pnl = _get_cumulative_pnl()
    drawdown_pct = _get_daily_drawdown_pct(equity)
    exposure = _get_market_exposure(policy)
    return {
        "equity": float(equity),
        "daily_pnl": float(daily_pnl),
        "cumulative_pnl": float(cumulative_pnl),
        "drawdown_pct": float(drawdown_pct),
        "exposure": float(exposure),
    }


def _get_cache_metrics(include: bool) -> Dict[str, int]:
    if not include:
        return {"hits": 0, "misses": 0, "expired": 0}
    stats = cache_module.stats()
    return {
        "hits": int(stats.get("hit", 0)),
        "misses": int(stats.get("miss", 0)),
        "expired": int(stats.get("expired", 0)),
    }


def build_report(
    policy: Dict[str, Any] | None = None,
    reset_counters: bool = False,
) -> Dict[str, Any]:
    """Construct a consolidated observability snapshot."""
    reporting = _reporting_cfg(policy)
    funnel_fields = reporting.get("funnel_fields", DEFAULT_FUNNEL_FIELDS)
    counters = metrics.get_all(reset=reset_counters)
    errors = int(counters.get("errors", 0))
    funnel = {field: int(counters.get(field, 0)) for field in funnel_fields}

    include_risk = reporting.get("include_risk_metrics", True)
    risk = (
        _collect_risk_metrics(policy or getattr(config, "_policy", {}) or {})
        if include_risk
        else {
            "equity": 0.0,
            "daily_pnl": 0.0,
            "cumulative_pnl": 0.0,
            "drawdown_pct": 0.0,
            "exposure": 0.0,
        }
    )

    cache_section = _get_cache_metrics(reporting.get("include_cache_metrics", True))

    now = datetime.utcnow()
    report = {
        "date": now.date().isoformat(),
        "generated_at": now.replace(microsecond=0).isoformat() + "Z",
        "funnel_fields": list(funnel_fields),
        "funnel": funnel,
        "risk": risk,
        "cache": cache_section,
        "errors": errors,
    }
    return report


def format_text(report: Dict[str, Any]) -> str:
    funnel_fields: Iterable[str] = report.get("funnel_fields", DEFAULT_FUNNEL_FIELDS)
    funnel = report.get("funnel", {})
    risk = report.get("risk", {})
    cache = report.get("cache", {})
    lines = [f"REPORT {report.get('date')}" ]
    lines.append(
        "Funnel: "
        + " ".join(f"{field}={int(funnel.get(field, 0))}" for field in funnel_fields)
    )
    lines.append(
        "Risk: "
        + f"equity={_fmt_currency(risk.get('equity'))} "
        + f"exposure={float(risk.get('exposure', 0.0)):.2f} "
        + f"daily_pnl={_fmt_signed_currency(risk.get('daily_pnl'))} "
        + f"cumulative_pnl={_fmt_signed_currency(risk.get('cumulative_pnl'))} "
        + f"drawdown={float(risk.get('drawdown_pct', 0.0)):+.2f}%"
    )
    lines.append(
        "Cache: "
        + f"hits={int(cache.get('hits', 0))} "
        + f"misses={int(cache.get('misses', 0))} "
        + f"expired={int(cache.get('expired', 0))}"
    )
    lines.append(f"Errors: {int(report.get('errors', 0))}")
    return "\n".join(lines)


def format_csv(report: Dict[str, Any]) -> str:
    funnel_fields: Iterable[str] = report.get("funnel_fields", DEFAULT_FUNNEL_FIELDS)
    risk = report.get("risk", {})
    cache = report.get("cache", {})
    output = io.StringIO()
    writer = csv.writer(output)
    header = [
        "date",
        *funnel_fields,
        "equity",
        "daily_pnl",
        "cumulative_pnl",
        "drawdown_pct",
        "exposure",
        "cache_hits",
        "cache_misses",
        "cache_expired",
        "errors",
    ]
    writer.writerow(header)
    row = [
        report.get("date"),
        *[int(report.get("funnel", {}).get(field, 0)) for field in funnel_fields],
        float(risk.get("equity", 0.0)),
        float(risk.get("daily_pnl", 0.0)),
        float(risk.get("cumulative_pnl", 0.0)),
        float(risk.get("drawdown_pct", 0.0)),
        float(risk.get("exposure", 0.0)),
        int(cache.get("hits", 0)),
        int(cache.get("misses", 0)),
        int(cache.get("expired", 0)),
        int(report.get("errors", 0)),
    ]
    writer.writerow(row)
    return output.getvalue()


def format_json(report: Dict[str, Any]) -> str:
    return json.dumps(report, indent=2, sort_keys=True)


def _append_history_row(report: Dict[str, Any], path: Path) -> None:
    funnel_fields: Iterable[str] = report.get("funnel_fields", DEFAULT_FUNNEL_FIELDS)
    risk = report.get("risk", {})
    cache = report.get("cache", {})
    fieldnames = [
        "date",
        *funnel_fields,
        "equity",
        "pnl",
        "exposure",
        "cache_hits",
        "cache_misses",
        "cache_expired",
        "errors",
    ]
    row = {
        "date": report.get("date"),
        **{field: int(report.get("funnel", {}).get(field, 0)) for field in funnel_fields},
        "equity": float(risk.get("equity", 0.0)),
        "pnl": float(risk.get("daily_pnl", 0.0)),
        "exposure": float(risk.get("exposure", 0.0)),
        "cache_hits": int(cache.get("hits", 0)),
        "cache_misses": int(cache.get("misses", 0)),
        "cache_expired": int(cache.get("expired", 0)),
        "errors": int(report.get("errors", 0)),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    file_exists = path.exists()
    with open(path, "a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)


def save_report_files(report: Dict[str, Any], directory: str) -> Dict[str, str]:
    base_path = Path(directory)
    base_path.mkdir(parents=True, exist_ok=True)
    csv_path = base_path / f"{report.get('date')}.csv"
    json_path = base_path / f"{report.get('date')}.json"
    csv_path.write_text(format_csv(report), encoding="utf-8")
    json_path.write_text(format_json(report), encoding="utf-8")
    history_path = base_path / "daily.csv"
    _append_history_row(report, history_path)
    return {
        "csv": str(csv_path),
        "json": str(json_path),
        "history": str(history_path),
    }
