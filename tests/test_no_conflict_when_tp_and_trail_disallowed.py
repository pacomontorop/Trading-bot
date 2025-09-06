def test_branch_when_broker_disallows_trailing_bracket(monkeypatch):
    from core.executor import should_use_combined_bracket
    import types

    cfg = {"exits": {"allow_tp_and_trailing_same_bracket": True}}
    broker_mod = types.SimpleNamespace(supports_bracket_trailing=lambda: False)

    assert not should_use_combined_bracket(cfg, broker_mod)

