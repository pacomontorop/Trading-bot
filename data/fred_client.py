import os
from typing import Any, Dict, List, Optional

import requests

BASE_URL = "https://api.stlouisfed.org/fred/series/observations"
API_KEY = os.getenv("FRED_API")


def _base_params(series_id: str) -> Dict[str, str]:
    if not API_KEY:
        raise RuntimeError("FRED_API environment variable not set")
    return {"series_id": series_id, "api_key": API_KEY, "file_type": "json"}


def get_series_observations(series_id: str, observation_start: Optional[str] = None, observation_end: Optional[str] = None, units: str = "lin") -> List[Dict[str, Any]]:
    """Retrieve observations for a given FRED series.

    Parameters
    ----------
    series_id:
        FRED series identifier (e.g. ``CPIAUCSL`` for CPI).
    observation_start, observation_end:
        Optional start/end dates in ``YYYY-MM-DD`` format.
    units:
        Transformation applied to data (see FRED docs).
    """
    params = _base_params(series_id)
    params["units"] = units
    if observation_start:
        params["observation_start"] = observation_start
    if observation_end:
        params["observation_end"] = observation_end
    response = requests.get(BASE_URL, params=params, timeout=10)
    response.raise_for_status()
    data = response.json()
    return data.get("observations", [])


def get_latest_value(series_id: str, units: str = "lin") -> Optional[float]:
    """Return the latest available numeric value for ``series_id``.

    Parameters
    ----------
    series_id:
        FRED series identifier.
    units:
        Optional transformation applied to the data (see FRED docs).
    """
    observations = get_series_observations(series_id, units=units)
    for obs in reversed(observations):
        val = obs.get("value")
        try:
            return float(val)
        except (TypeError, ValueError):
            continue
    return None


MACRO_SERIES = {
    # key -> (series_id, units)
    "inflation": ("CPIAUCSL", "pc1"),  # Year-over-year % change
    "unemployment": ("UNRATE", "lin"),  # Civilian Unemployment Rate
    "fed_funds": ("FEDFUNDS", "lin"),  # Effective Federal Funds Rate
    "gdp_real": ("GDPC1", "pca"),  # Real GDP, annual rate of change
}


def get_macro_snapshot() -> Dict[str, Optional[float]]:
    """Return a snapshot of selected macroeconomic series."""
    snapshot: Dict[str, Optional[float]] = {}
    for name, (series_id, units) in MACRO_SERIES.items():
        snapshot[name] = get_latest_value(series_id, units=units)
    return snapshot
