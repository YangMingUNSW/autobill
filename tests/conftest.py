import pytest


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    """Every test gets a fresh, empty AUTOBILL_DATA_DIR; never the real database."""
    data_dir = tmp_path / "autobill-data"
    data_dir.mkdir()
    monkeypatch.setenv("AUTOBILL_DATA_DIR", str(data_dir))
    return data_dir
