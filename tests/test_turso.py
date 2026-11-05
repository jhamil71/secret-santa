"""The Turso (hosted database) path, against a fake Turso server."""
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dbconn  # noqa: E402
from fake_turso import FakeTurso  # noqa: E402


@pytest.fixture()
def turso(tmp_path, monkeypatch):
    fake = FakeTurso(str(tmp_path / "t.db"))
    monkeypatch.setenv("TURSO_DATABASE_URL", fake.url)
    monkeypatch.setenv("TURSO_AUTH_TOKEN", "secret-token")
    yield fake
    fake.stop()


def test_reads_writes_transactions_and_rows(turso):
    c = dbconn.connect("unused")
    c.executescript("CREATE TABLE t(id INTEGER PRIMARY KEY, name TEXT UNIQUE, n REAL, b BLOB);")
    c.execute("INSERT INTO t(name, n, b) VALUES (?, ?, ?)", ("ann", 1.5, b"\x00\x01\xff"))
    c.execute("INSERT INTO t(name, b) VALUES (?, ?)", ("one-byte-short", b"ab"))   # base64 lengths that need padding
    c.execute("INSERT INTO t(name, b) VALUES (?, ?)", ("one-byte", b"a"))
    c.commit()
    assert [r["b"] for r in c.execute("SELECT b FROM t WHERE name LIKE 'one%' ORDER BY name")] == [b"a", b"ab"]
    c.execute("DELETE FROM t WHERE name LIKE 'one%'")
    c.commit()
    row = c.execute("SELECT * FROM t WHERE name=?", ("ann",)).fetchone()
    assert row["name"] == "ann" and row[0] == 1 and row["n"] == 1.5 and row["b"] == b"\x00\x01\xff"
    assert dict(row)["name"] == "ann" and tuple(row)[1] == "ann"
    assert c.execute("SELECT * FROM t WHERE name='nobody'").fetchone() is None
    assert [r["name"] for r in c.execute("SELECT * FROM t")] == ["ann"]
    c.close()

    # an uncommitted change is thrown away when the connection closes (like sqlite3)
    c = dbconn.connect("unused")
    c.execute("INSERT INTO t(name) VALUES ('ghost')")
    assert c.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 2     # visible inside its own transaction
    c.close()
    assert dbconn.connect("unused").execute("SELECT COUNT(*) FROM t").fetchone()[0] == 1

    # a committed change sticks, and a constraint failure is an IntegrityError
    c = dbconn.connect("unused")
    c.execute("INSERT INTO t(name) VALUES ('bob')")
    c.commit()
    with pytest.raises(dbconn.IntegrityError):
        c.execute("INSERT INTO t(name) VALUES ('bob')")
    c.rollback()
    assert dbconn.connect("unused").execute("SELECT COUNT(*) FROM t").fetchone()[0] == 2


def test_bad_token_and_unreachable_server(turso, monkeypatch):
    monkeypatch.setenv("TURSO_AUTH_TOKEN", "wrong")
    with pytest.raises(dbconn.OperationalError):
        dbconn.connect("unused").execute("SELECT 1")
    monkeypatch.setenv("TURSO_DATABASE_URL", "http://127.0.0.1:9")
    with pytest.raises(dbconn.OperationalError):
        dbconn.connect("unused").execute("SELECT 1")


def test_pictures_are_stored_in_and_served_from_the_database(turso, tmp_path, monkeypatch):
    import app as app_module
    monkeypatch.setattr(app_module, "DB_PATH", str(tmp_path / "t.db"))
    app_module.init_db()
    client = app_module.app.test_client()
    from test_preview import PNG
    with app_module.app.test_request_context():
        app_module.db()
        app_module._save_image("a" * 40 + ".png", "png", PNG)
        app_module.db().close()
    r = client.get("/img/" + "a" * 40 + ".png")
    assert r.status_code == 200 and r.data == PNG and r.mimetype == "image/png"
    assert "immutable" in r.headers["Cache-Control"]
    assert client.get("/img/" + "b" * 40 + ".png").status_code == 404
    assert client.get("/img/../etc/passwd").status_code == 404
