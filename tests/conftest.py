"""Shared fixtures. Each API test gets a fresh temp DB and clean in-memory
state; the pkuseg model and CC-CEDICT load once per process."""
import os

import pytest

os.environ["HSKLYRICS_SECURE_COOKIES"] = "0"

SONG = """月亮代表我的心
你问我爱你有多深
轰轰烈烈的爱情
baby 我唱歌给你听"""


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app import db, main

    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.db"))
    main._buckets.clear()
    with TestClient(main.app) as c:
        yield c


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    from app import db

    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "unit.db"))
    db.init()
    conn = db.connect()
    yield conn
    conn.close()


def register(client, email="alice@example.com", password="hunter2secret"):
    r = client.post("/api/register", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()
