"""Test-wide isolation from the developer's ``.env``.

``Settings`` is declared with ``env_file=".env"``, so a real local config — e.g.
``NOTELINKS_JUDGE_PROVIDER=ollama`` set for actual use — would silently bleed
into every ``Settings()``-based test and make results depend on the machine the
suite runs on (clearing the env var isn't enough; the dotenv file is still read).

Disable dotenv loading for every test so the suite is hermetic. Tests that need
specific configuration set it explicitly (constructor kwargs or
``monkeypatch.setenv``), which still works — only the ``.env`` *file* is ignored.
"""

import pytest

from notelinks.config import Settings


@pytest.fixture(autouse=True)
def _no_dotenv(monkeypatch):
    monkeypatch.setitem(Settings.model_config, "env_file", None)
