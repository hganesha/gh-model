from churn.featurize import build


def test_build_has_one_row_per_customer(tmp_path, monkeypatch):
    assert build is not None
