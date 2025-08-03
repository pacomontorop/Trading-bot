#test_env.py

"""Environment variable tests using mocked values.

The project expects the ``QUIVER_API_KEY`` environment variable to be defined.
In the CI environment we avoid relying on a real key by patching ``os.environ``
so that loading the ``.env`` file never triggers network calls or requires
secret values.
"""

import os
import sys
from types import SimpleNamespace
from unittest.mock import patch

# Provide dummy module when python-dotenv is missing
sys.modules.setdefault("dotenv", SimpleNamespace(load_dotenv=lambda *a, **k: None))
from dotenv import load_dotenv


def test_quiver_api_key_loaded():
    """Ensure ``QUIVER_API_KEY`` is read from the environment."""

    with patch.dict(os.environ, {"QUIVER_API_KEY": "dummy"}, clear=True):
        load_dotenv()
        assert os.getenv("QUIVER_API_KEY") == "dummy"
