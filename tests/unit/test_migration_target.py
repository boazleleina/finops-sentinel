"""The app and Alembic must resolve the same database file.

This exists because they once did not. `alembic/env.py` read SENTINEL_DB_PATH
straight from os.environ with its own ".sentinel.db" default, while the app
read it through Settings — which also reads .env. The documented setup puts
SENTINEL_DB_PATH in .env, so migrations landed on .sentinel.db while the app
used data/sentinel.db. Nothing failed loudly: the app simply ran against a
schema that was being migrated somewhere else, until it reached a table that
had never been created in its own file.
"""
import configparser
from pathlib import Path

from finops_sentinel.bootstrap import get_repository
from finops_sentinel.config import database_url, settings

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_the_app_and_alembic_build_the_url_the_same_way():
    """Both call config.database_url. If this ever forks again, so does the
    schema — silently, which is what made the original bug expensive."""
    assert get_repository().db_url == database_url()


def test_database_url_follows_the_configured_path(monkeypatch):
    monkeypatch.setattr(settings, "sentinel_db_path", "data/elsewhere.db")

    assert database_url() == "sqlite:///data/elsewhere.db"
    assert get_repository().db_url == "sqlite:///data/elsewhere.db"


def test_alembic_ini_does_not_hardcode_a_database():
    """A URL here reads as authoritative and is not: env.py overwrites it.
    That dead config is precisely what disagreed with .env for months."""
    parser = configparser.ConfigParser()
    parser.read(REPO_ROOT / "alembic.ini")

    assert parser.get("alembic", "sqlalchemy.url", fallback="").strip() == ""


def test_alembic_env_does_not_read_the_environment_itself():
    """The fix is env.py delegating to config.database_url. Reading os.getenv
    there is how the two sources of truth came apart in the first place."""
    env_py = (REPO_ROOT / "alembic" / "env.py").read_text()

    assert "database_url" in env_py
    # The call, not the word — the docstring explains the old bug by name.
    assert "os.getenv(" not in env_py
