from external_providers import _team, _int, _float, _date

def test_provider_normalizers():
    assert _team("JAX") == "JAC"
    assert _team("LA") == "LAR"
    assert _int("12.0") == 12
    assert _float("1.25") == 1.25
    assert str(_date("2025-09-07T12:00:00Z")) == "2025-09-07"
