import pytest


@pytest.fixture(autouse=True)
def isolated_app(tmp_path, monkeypatch):
    """Every test gets its own ~/.dbide, in-memory secrets, and fresh singletons."""
    monkeypatch.setenv("DBIDE_HOME", str(tmp_path / "dbide_home"))
    from dbide.core import roles, secrets, settings

    settings.reset_settings_for_tests()
    settings.settings().set("beginner_mode", False)
    secrets.set_store(secrets.SessionStore())
    roles.set_role_store(None)
    try:
        from dbide.ui.help_panel import HelpCenter

        HelpCenter._instance = None
    except ImportError:
        pass
    yield
    secrets.set_store(None)
    roles.set_role_store(None)
    settings.reset_settings_for_tests()
