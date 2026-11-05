"""A stand-in for Turso's HTTP API (/v2/pipeline), backed by a local SQLite file.

It follows the documented request/response shapes (typed values, batons that keep a connection and its
transaction alive between requests, 'close' ending it), so the app's Turso code can be tested without internet.
"""
import base64
import json
import sqlite3
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _enc(v):
    if v is None:
        return {"type": "null"}
    if isinstance(v, int):
        return {"type": "integer", "value": str(v)}
    if isinstance(v, float):
        return {"type": "float", "value": v}
    if isinstance(v, bytes):
        return {"type": "blob", "base64": base64.b64encode(v).decode().rstrip("=")}   # like the real server: no padding
    return {"type": "text", "value": v}


def _dec(c):
    t = c["type"]
    if t == "null":
        return None
    if t == "integer":
        return int(c["value"])
    if t == "float":
        return c["value"]
    if t == "blob":
        return base64.b64decode(c["base64"])
    return c["value"]


class FakeTurso:
    def __init__(self, db_path, token="secret-token"):
        self.db_path, self.token = db_path, token
        self.sessions = {}
        self.requests = 0                 # HTTP requests served
        self.lock = threading.Lock()
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                outer.requests += 1
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.headers.get("Authorization") != f"Bearer {outer.token}":
                    return self._send(401, {"error": "Unauthorized"})
                if self.path != "/v2/pipeline":
                    return self._send(404, {"error": "not found"})
                self._send(200, outer.pipeline(body))

            def _send(self, code, obj):
                data = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        self.server.shutdown()
        for c in self.sessions.values():
            c.close()

    def pipeline(self, body):
        baton = body.get("baton")
        with self.lock:
            conn = self.sessions.get(baton) if baton else None
            if conn is None:
                conn = sqlite3.connect(self.db_path, isolation_level=None, check_same_thread=False)
                conn.execute("PRAGMA foreign_keys=OFF")
        results, closed = [], False
        for req in body["requests"]:
            if req["type"] == "close":
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                conn.close()
                closed = True
                results.append({"type": "ok", "response": {"type": "close"}})
                continue
            st = req["stmt"]
            if getattr(self, 'trace', None) is not None: self.trace.append(st['sql'][:90])
            args = [_dec(a) for a in st.get("args", [])]
            try:
                cur = conn.execute(st["sql"], args)
                cols = [{"name": d[0], "decltype": None} for d in (cur.description or [])]
                rows = [[_enc(v) for v in r] for r in cur.fetchall()] if cur.description else []
                results.append({"type": "ok", "response": {"type": "execute", "result": {
                    "cols": cols, "rows": rows, "affected_row_count": max(cur.rowcount, 0),
                    "last_insert_rowid": str(cur.lastrowid) if cur.lastrowid else None}}})
            except sqlite3.Error as e:
                results.append({"type": "error", "error": {"message": str(e), "code": "SQLITE_ERROR"}})
        new_baton = None
        if not closed:
            new_baton = baton or uuid.uuid4().hex
            with self.lock:
                self.sessions[new_baton] = conn
        elif baton:
            with self.lock:
                self.sessions.pop(baton, None)
        return {"baton": new_baton, "base_url": None, "results": results}
