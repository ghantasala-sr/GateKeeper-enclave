"""Liveness vs readiness, and keeping the owner's password out of the running API.

Kubernetes restarts a pod whose liveness probe fails and stops sending traffic
to one whose readiness probe fails. A database outage must cause the second,
never the first: restarting every pod can't fix the database.
"""

import asyncio

import pytest
from fastapi.testclient import TestClient

from app import db, main


@pytest.fixture(scope="module")
def client():
    with TestClient(main.app) as c:  # runs lifespan: DB pool + embedding model
        yield c


def test_health_and_ready_when_all_is_well(client):
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/ready").json() == {"status": "ready"}


def test_version_reports_the_release(client, monkeypatch):
    monkeypatch.setattr(main.settings, "app_version", "1.2.3")
    assert client.get("/version").json() == {"version": "1.2.3"}


class BrokenPool:
    """A pool whose database has gone away."""

    def acquire(self):
        raise OSError("connection refused")


class HangingPool:
    """A pool whose database accepts connections but never answers."""

    def acquire(self):
        class Conn:
            async def __aenter__(self):
                await asyncio.sleep(60)

            async def __aexit__(self, *exc):
                return False
        return Conn()


@pytest.mark.parametrize("pool", [BrokenPool(), HangingPool()])
def test_database_outage_fails_readiness_not_liveness(client, monkeypatch, pool):
    monkeypatch.setattr(db, "pool", pool)
    resp = client.get("/ready")
    assert resp.status_code == 503 and resp.json() == {"status": "database unavailable"}
    assert client.get("/health").status_code == 200   # so Kubernetes doesn't restart the pod


def test_api_starts_without_the_owner_password(monkeypatch):
    """Only the setup scripts need MIGRATION_DATABASE_URL; they say so clearly when it's missing."""
    monkeypatch.setattr(db.settings, "migration_database_url", "")
    with pytest.raises(SystemExit, match="only the setup scripts need it"):
        asyncio.run(db.connect_as_owner())


def test_settings_from_mounted_secret_files(tmp_path, monkeypatch):
    """GKE's Secret Manager CSI driver mounts each secret as a file; the URLs are built from parts."""
    from app.config import Settings
    for name, value in {"app_db_password": "p@ss/word", "owner_password": "own3r", "session_secret": "s",
                        "token_signing_key_b64": "k"}.items():
        (tmp_path / name).write_text(value)
    for var in ("APP_DATABASE_URL", "MIGRATION_DATABASE_URL", "APP_DB_PASSWORD", "SESSION_SECRET", "TOKEN_SIGNING_KEY_B64"):
        monkeypatch.delenv(var, raising=False)
    s = Settings(_env_file=None, _secrets_dir=tmp_path, database_host="10.50.0.3")
    assert s.app_database_url == "postgresql://gatekeeper_app:p%40ss%2Fword@10.50.0.3:5432/gatekeeper"
    assert s.migration_database_url == "postgresql://gatekeeper_owner:own3r@10.50.0.3:5432/gatekeeper"
    assert s.session_secret == "s"
