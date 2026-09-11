"""El bot de Render está apagado por defecto (2026-09-11): no arranca el scheduler salvo RENDER_BOT_ENABLED=1."""
import importlib
import sys
from unittest.mock import patch


def _load(env):
    with patch.dict("os.environ", env, clear=False):
        sys.modules.pop("start", None)
        return importlib.import_module("start")


def test_apagado_por_defecto_no_arranca_scheduler():
    start = _load({"RENDER_BOT_ENABLED": "0", "WEB_CONCURRENCY": "1"})
    with patch("start.threading.Thread") as th, patch("utils.telegram_alert.send_telegram_alert", return_value=True):
        start._start_scheduler()
    assert th.call_count == 0
    assert start.healthcheck() == {"status": "ok", "bot": "disabled"}


def test_se_puede_reactivar_con_variable():
    start = _load({"RENDER_BOT_ENABLED": "1", "WEB_CONCURRENCY": "1"})
    with patch("start.threading.Thread") as th:
        start._start_scheduler()
    assert th.call_count == 1
    assert start.healthcheck()["bot"] == "enabled"
