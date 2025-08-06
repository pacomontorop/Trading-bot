import pandas as pd
from signals import reader


def test_get_trade_history_score_bonus(tmp_path, monkeypatch):
    file = tmp_path / "orders_history.csv"
    df = pd.DataFrame(
        [
            {
                "symbol": "AAA",
                "tipo": "long",
                "precio_entrada": 10.0,
                "precio_salida": 12.0,
                "shares": 1,
                "resultado": "ganadora",
            },
            {
                "symbol": "AAA",
                "tipo": "short",
                "precio_entrada": 15.0,
                "precio_salida": 10.0,
                "shares": 1,
                "resultado": "ganadora",
            },
        ]
    )
    df.to_csv(file, index=False)
    monkeypatch.setattr(reader, "ORDERS_HISTORY_FILE", str(file))
    assert reader.get_trade_history_score("AAA") == 2
