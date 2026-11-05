"""Database connection for the site.

Normally this is an ordinary local SQLite file. When TURSO_DATABASE_URL is set (for hosting on Vercel,
which has no permanent disk), the same SQL runs against a free hosted Turso database over HTTPS instead.
Turso is SQLite underneath, so the rest of the app doesn't change: this module just gives the app a
connection object that behaves like sqlite3's (execute, fetchone, fetchall, commit, rows you can read
by column name).
"""
import base64
import os
import re
import sqlite3

import requests

IntegrityError = sqlite3.IntegrityError
OperationalError = sqlite3.OperationalError

_session = requests.Session()
_WRITE_VERBS = {"INSERT", "UPDATE", "DELETE", "REPLACE"}


def turso_configured():
    return bool(os.environ.get("TURSO_DATABASE_URL"))


def connect(path, timeout=10):
    if turso_configured():
        return TursoConnection(os.environ["TURSO_DATABASE_URL"], os.environ.get("TURSO_AUTH_TOKEN", ""))
    c = sqlite3.connect(path, timeout=timeout)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    return c


class Row(tuple):
    """A result row: read it by position or by column name, like sqlite3.Row."""

    def __new__(cls, values, index):
        row = super().__new__(cls, values)
        row._index = index
        return row

    def __getitem__(self, key):
        if isinstance(key, str):
            return tuple.__getitem__(self, self._index[key])
        return tuple.__getitem__(self, key)

    def keys(self):
        return sorted(self._index, key=self._index.get)


class Cursor:
    def __init__(self, cols=(), rows=(), rowcount=-1, lastrowid=None):
        index = {}
        for i, name in enumerate(cols):
            index.setdefault(name, i)
        self._rows = [Row(r, index) for r in rows]
        self._pos = 0
        self.rowcount = rowcount
        self.lastrowid = lastrowid

    def fetchone(self):
        if self._pos >= len(self._rows):
            return None
        self._pos += 1
        return self._rows[self._pos - 1]

    def fetchall(self):
        rest = self._rows[self._pos:]
        self._pos = len(self._rows)
        return rest

    def __iter__(self):
        return iter(self.fetchall())


def _encode(v):
    if v is None:
        return {"type": "null"}
    if isinstance(v, bool):
        return {"type": "integer", "value": str(int(v))}
    if isinstance(v, int):
        return {"type": "integer", "value": str(v)}
    if isinstance(v, float):
        return {"type": "float", "value": v}
    if isinstance(v, (bytes, bytearray, memoryview)):
        return {"type": "blob", "base64": base64.b64encode(bytes(v)).decode()}
    return {"type": "text", "value": str(v)}


def _decode(cell):
    t = cell.get("type")
    if t == "null":
        return None
    if t == "integer":
        return int(cell["value"])
    if t == "float":
        return float(cell["value"])
    if t == "blob":
        b64 = cell.get("base64", "")
        return base64.b64decode(b64 + "=" * (-len(b64) % 4))      # Turso sends it without the trailing '=' padding
    return cell.get("value")


def _verb(sql):
    m = re.match(r"\s*(\w+)", sql)
    return m.group(1).upper() if m else ""


class TursoConnection:
    """Just enough of sqlite3.Connection for this app, speaking Turso's HTTP API.

    Reads and schema changes are single requests. The first INSERT/UPDATE/DELETE opens a transaction
    on a server-side connection (kept alive by a 'baton'), which commit() finishes, like sqlite3 does.
    """

    def __init__(self, url, token):
        self.base = re.sub(r"^(libsql|turso|wss?)://", "https://", url.strip()).rstrip("/")
        self.token = token
        self.baton = None
        self.in_tx = False

    # -- wire ------------------------------------------------------------------------------------
    def _stmt(self, sql, params=()):
        s = {"sql": sql}
        if params:
            s["args"] = [_encode(p) for p in params]
        return {"type": "execute", "stmt": s}

    def _send(self, reqs, close):
        body = {"baton": self.baton, "requests": list(reqs) + ([{"type": "close"}] if close else [])}
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        try:
            r = _session.post(f"{self.base}/v2/pipeline", json=body, headers=headers, timeout=(5, 25))
        except requests.RequestException as e:
            raise OperationalError(f"Could not reach the database: {e}")
        if r.status_code != 200:
            raise OperationalError(f"Database error {r.status_code}: {r.text[:300]}")
        data = r.json()
        self.baton = None if close else data.get("baton")
        if data.get("base_url") and not close:
            self.base = data["base_url"].rstrip("/")
        return data.get("results", [])

    @staticmethod
    def _raise(err):
        msg = (err.get("error") or {}).get("message", "database error")
        if "constraint" in msg.lower():
            raise IntegrityError(msg)
        raise OperationalError(msg)

    def _results(self, results, n):
        """Check the first n results; return the last one's execute result."""
        out = None
        failure = None
        for res in results[:n]:
            if res.get("type") == "error":
                failure = failure or res
            else:
                out = res["response"].get("result", {})
        if failure:
            self._raise(failure)
        return out or {}

    def _cursor(self, result):
        lri = result.get("last_insert_rowid")
        return Cursor([c.get("name") for c in result.get("cols", [])],
                      [[_decode(c) for c in row] for row in result.get("rows", [])],
                      result.get("affected_row_count", -1), int(lri) if lri not in (None, "") else None)

    # -- sqlite3-like API --------------------------------------------------------------------------
    def execute(self, sql, params=()):
        verb = _verb(sql)
        if verb == "PRAGMA" and "foreign_keys" in sql.lower():
            return Cursor()                       # the server enforces this per transaction below
        if self.in_tx:
            return self._cursor(self._results(self._send([self._stmt(sql, params)], close=False), 1))
        if verb in _WRITE_VERBS:
            reqs = [self._stmt("PRAGMA foreign_keys=ON"), self._stmt("BEGIN"), self._stmt(sql, params)]
            self.in_tx = True
            return self._cursor(self._results(self._send(reqs, close=False), 3))
        return self._cursor(self._results(self._send([self._stmt(sql, params)], close=True), 1))

    def executescript(self, script):
        stmts = [s.strip() for s in script.split(";") if s.strip()]
        reqs = [self._stmt(s) for s in stmts]
        self._results(self._send(reqs, close=True), len(reqs))

    def commit(self):
        if self.in_tx:
            self.in_tx = False
            self._results(self._send([self._stmt("COMMIT")], close=True), 1)

    def rollback(self):
        if self.in_tx:
            self.in_tx = False
            try:
                self._send([self._stmt("ROLLBACK")], close=True)
            except OperationalError:
                pass

    def close(self):
        if self.in_tx:
            self.rollback()                       # an uncommitted change is thrown away, as in sqlite3
