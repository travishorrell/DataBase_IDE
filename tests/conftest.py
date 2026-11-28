import pytest


@pytest.fixture(autouse=True)
def isolated_app(tmp_path, monkeypatch):
    """Every test gets its own ~/.dbide, in-memory secrets, and fresh singletons."""
    monkeypatch.setenv("DBIDE_HOME", str(tmp_path / "dbide_home"))
    from dbide.core import roles, secrets, settings

    settings.reset_settings_for_tests()
    settings.settings().set("beginner_mode", False)
    settings.settings().set("mode", "learning")  # mode tests switch to power explicitly
    settings.settings().set("first_run_done", True)  # the first-launch test turns this off itself
    secrets.set_store(secrets.SessionStore())
    roles.set_role_store(None)
    from dbide.core import playground, progress

    playground.reset_for_tests()
    progress.reset_for_tests()
    # existing tests exercise every feature: start them at the expert level (progress tests reset this)
    settings.settings().set("progress", {"skills": {}, "override": 3})
    try:
        from dbide.ui.help_panel import HelpCenter

        HelpCenter._instance = None
    except ImportError:
        pass
    yield
    secrets.set_store(None)
    roles.set_role_store(None)
    settings.reset_settings_for_tests()
