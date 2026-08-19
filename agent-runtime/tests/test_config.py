"""Config: .env defaults load; real environment always wins."""

import os

from ratsnest.config import _apply_dotenv


def test_dotenv_values_become_defaults(tmp_path, monkeypatch):
    monkeypatch.delenv("RATSNEST_TEST_ALPHA", raising=False)
    monkeypatch.delenv("RATSNEST_TEST_QUOTED", raising=False)
    env = tmp_path / ".env"
    env.write_text(
        "# comment line\n"
        "\n"
        "RATSNEST_TEST_ALPHA=from-file\n"
        "RATSNEST_TEST_QUOTED=\"quoted value\"\n"
        "not a valid line\n",
        encoding="utf-8")
    _apply_dotenv(env)
    assert os.environ["RATSNEST_TEST_ALPHA"] == "from-file"
    assert os.environ["RATSNEST_TEST_QUOTED"] == "quoted value"
    monkeypatch.delenv("RATSNEST_TEST_ALPHA", raising=False)
    monkeypatch.delenv("RATSNEST_TEST_QUOTED", raising=False)


def test_real_environment_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("RATSNEST_TEST_BETA", "from-env")
    env = tmp_path / ".env"
    env.write_text("RATSNEST_TEST_BETA=from-file\n", encoding="utf-8")
    _apply_dotenv(env)
    assert os.environ["RATSNEST_TEST_BETA"] == "from-env"


def test_missing_file_is_a_noop(tmp_path):
    _apply_dotenv(tmp_path / "nope.env")  # must not raise
