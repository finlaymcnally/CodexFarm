import pytest


@pytest.fixture(autouse=True)
def _skip_login_precheck_in_tests(monkeypatch):
    monkeypatch.setenv("CODEX_FARM_SKIP_LOGIN_PRECHECK", "1")
