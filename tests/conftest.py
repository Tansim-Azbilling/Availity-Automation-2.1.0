"""Isolate persisted GUI settings from the developer's AppData folder."""
import pytest

from availity_app.user_settings import set_settings_path_for_tests


@pytest.fixture(autouse=True)
def _isolate_user_settings(tmp_path):
    set_settings_path_for_tests(str(tmp_path / 'settings.json'))
    yield
    set_settings_path_for_tests(None)
