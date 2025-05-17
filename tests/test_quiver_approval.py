# tests/test_quiver_approval.py
from signals.quiver_approval import is_approved_by_quiver

def test_quiver_integrity_response():
    result = is_approved_by_quiver("AAPL")
    assert isinstance(result, bool), "La función no devuelve un booleano"
  
def test_quiver_with_fake_symbol():
    result = is_approved_by_quiver("FAKE1234")
    assert isinstance(result, bool), "Error en símbolo inexistente"
