"""Family Secret Santa - a small self-hosted gift exchange."""
import os
import re
import secrets
import sqlite3
import time
from datetime import datetime
from functools import wraps
from urllib.parse import urlparse

from flask import (Flask, abort, flash, g, jsonify, redirect, render_template,
                   request, session, url_for)

from draw import find_assignment
from preview import fetch_preview

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(APP_DIR, "data"))
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "santa.db")


def _persisted_secret(filename, nbytes):
    path = os.path.join(DATA_DIR, filename)
    if os.path.exists(path):
        with open(path) as f:
            return f.read().strip(), False
    value = secrets.token_urlsafe(nbytes)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(value)
    return value, True


SECRET_KEY, _ = _persisted_secret("secret_key.txt", 32)
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")
if not ADMIN_PASSWORD:
    ADMIN_PASSWORD, generated = _persisted_secret("admin_password.txt", 9)
    if generated:
        print(f"\n*** No ADMIN_PASSWORD set. Generated one: {ADMIN_PASSWORD} "
              f"(saved in {DATA_DIR}/admin_password.txt) ***\n", flush=True)

app = Flask(__name__)
app.config.update(
    SECRET_KEY=SECRET_KEY,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE") == "1",
    PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 30,
)

if os.environ.get("TRUST_PROXY") == "1":
    # Behind a reverse proxy / port forwarder: use its host and https so links come out right.
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

SCHEMA = """
CREATE TABLE IF NOT EXISTS people(
    id INTEGER PRIMARY KEY, name TEXT NOT NULL, phone TEXT, email TEXT, token TEXT NOT NULL UNIQUE);
CREATE TABLE IF NOT EXISTS exclusions(
    giver_id INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    receiver_id INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    PRIMARY KEY(giver_id, receiver_id));
CREATE TABLE IF NOT EXISTS history(
    year INTEGER NOT NULL,
    giver_id INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    receiver_id INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    PRIMARY KEY(year, giver_id));
CREATE TABLE IF NOT EXISTS assignments(
    giver_id INTEGER PRIMARY KEY REFERENCES people(id) ON DELETE CASCADE,
    receiver_id INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS items(
    id INTEGER PRIMARY KEY,
    person_id INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    title TEXT NOT NULL, url TEXT, price TEXT, image TEXT, note TEXT,
    purchased_by INTEGER REFERENCES people(id) ON DELETE SET NULL,
    created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
"""
DEFAULTS = {
    "year": str(datetime.now().year), "budget": "", "event_note": "",
    "avoid_swaps": "1", "avoid_last": "1", "drawn_at": "",
}


def init_db():
    with sqlite3.connect(DB_PATH) as c:
        c.executescript(SCHEMA)
        # Upgrade databases created by earlier versions.
        if "phone" not in {r[1] for r in c.execute("PRAGMA table_info(people)")}:
            c.execute("ALTER TABLE people ADD COLUMN phone TEXT")
        icols = {r[1] for r in c.execute("PRAGMA table_info(items)")}
        if "purchased_by" not in icols:
            c.execute("ALTER TABLE items ADD COLUMN purchased_by INTEGER "
                      "REFERENCES people(id) ON DELETE SET NULL")
            if "purchased" in icols:
                c.execute("UPDATE items SET purchased_by=(SELECT giver_id FROM assignments "
                          "WHERE receiver_id=items.person_id) WHERE purchased=1")
        for k, v in DEFAULTS.items():
            c.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, v))


init_db()


def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH, timeout=10)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    conn = g.pop("db", None)
    if conn:
        conn.close()


def get_setting(key):
    row = db().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else DEFAULTS.get(key, "")


def set_setting(key, value):
    db().execute("INSERT INTO settings(key, value) VALUES(?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


def is_drawn():
    return db().execute("SELECT 1 FROM assignments LIMIT 1").fetchone() is not None


# ---------- helpers ----------
def clean_text(s, limit):
    return (s or "").strip()[:limit]


def clean_phone(s):
    """Digits with an optional leading +, 7-15 digits. Returns None if it doesn't look like a number."""
    s = (s or "").strip()
    if not s or not re.fullmatch(r"\+?[\d\s().\-]+", s):
        return None
    digits = re.sub(r"\D", "", s)
    if not 7 <= len(digits) <= 15:
        return None
    return ("+" if s.startswith("+") else "") + digits


def clean_url(s):
    """Return a safe http(s) URL or None. (Blocks javascript: etc.)"""
    s = (s or "").strip()
    if not s:
        return None
    if "://" not in s:
        if re.match(r"^(javascript|data|vbscript|file|ftp|mailto|tel):", s, re.I):
            return None
        s = "https://" + s
    p = urlparse(s)
    try:
        p.port  # raises ValueError on junk like "https://javascript:alert(1)"
    except ValueError:
        return None
    if p.scheme not in ("http", "https") or not p.hostname or len(s) > 2000:
        return None
    return s


def base_url():
    return (os.environ.get("BASE_URL") or request.host_url).rstrip("/")


def person_link(token):
    return f"{base_url()}{url_for('person_page', token=token)}"


def sms_text(name, token):
    return (f"Hi {name}! Here's your private link for our family Secret Santa: {person_link(token)} "
            "Add your wish list there, and once names are drawn you'll see who you're buying for. "
            "Please keep this link to yourself!")


@app.before_request
def csrf_protect():
    if request.method == "POST":
        sent = request.form.get("csrf") or request.headers.get("X-CSRF") or ""
        if not secrets.compare_digest(sent, session.get("csrf", "")):
            abort(400, "Your session expired. Go back, refresh the page and try again.")


@app.context_processor
def inject_globals():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(24)
    return {"csrf": session["csrf"]}


def admin_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not session.get("admin"):
            return redirect(url_for("admin_login"))
        return fn(*a, **kw)
    return wrapper


_attempts = {}


def too_many_attempts(ip):
    now = time.time()
    recent = [t for t in _attempts.get(ip, []) if now - t < 300]
    _attempts[ip] = recent
    return len(recent) >= 10


# ---------- public ----------
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        ip = request.remote_addr or "?"
        if too_many_attempts(ip):
            flash("Too many attempts. Wait a few minutes and try again.", "error")
            return render_template("admin_login.html"), 429
        pw = request.form.get("password", "")
        if secrets.compare_digest(pw.encode(), ADMIN_PASSWORD.encode()):
            session["admin"] = True
            session.permanent = True
            return redirect(url_for("admin"))
        _attempts.setdefault(ip, []).append(time.time())
        flash("Wrong password.", "error")
    return render_template("admin_login.html")


@app.post("/admin/logout")
def admin_logout():
    session.pop("admin", None)
    return redirect(url_for("index"))


# ---------- moderator ----------
@app.route("/admin")
@admin_required
def admin():
    d = db()
    people = d.execute(
        "SELECT p.*, (SELECT COUNT(*) FROM items i WHERE i.person_id=p.id) AS n_items "
        "FROM people p ORDER BY LOWER(p.name)").fetchall()
    names = {p["id"]: p["name"] for p in people}
    year = int(get_setting("year"))

    pairs = {(r["giver_id"], r["receiver_id"])
             for r in d.execute("SELECT giver_id, receiver_id FROM exclusions")}
    rules, seen = [], set()
    for a, b in sorted(pairs):
        if (b, a) in pairs:
            key = frozenset((a, b))
            if key in seen:
                continue
            seen.add(key)
            rules.append({"a": a, "b": b, "both": True})
        else:
            rules.append({"a": a, "b": b, "both": False})

    last = {r["giver_id"]: r["receiver_id"] for r in d.execute(
        "SELECT giver_id, receiver_id FROM history WHERE year=?", (year - 1,))}
    return render_template(
        "admin.html", people=people, names=names, rules=rules, last=last, year=year,
        drawn=is_drawn(), drawn_at=get_setting("drawn_at"),
        settings={k: get_setting(k) for k in DEFAULTS},
        link_for=person_link, sms_text=sms_text)


@app.post("/admin/people")
@admin_required
def add_person():
    d = db()
    if is_drawn():
        flash("Names are already drawn. Reset the draw before changing who is playing.", "error")
        return redirect(url_for("admin"))
    name = clean_text(request.form.get("name"), 60)
    if not name:
        flash("Please enter a name.", "error")
        return redirect(url_for("admin"))
    raw_phone = request.form.get("phone", "")
    phone = clean_phone(raw_phone)
    if raw_phone.strip() and not phone:
        flash("That phone number doesn't look right. Use digits only, adding a country code like +1 if needed.", "error")
        return redirect(url_for("admin"))
    d.execute("INSERT INTO people(name, phone, email, token) VALUES (?, ?, ?, ?)",
              (name, phone, clean_text(request.form.get("email"), 120) or None,
               secrets.token_urlsafe(12)))
    d.commit()
    flash(f"Added {name}.", "ok")
    return redirect(url_for("admin"))


@app.post("/admin/people/<int:pid>/delete")
@admin_required
def delete_person(pid):
    d = db()
    if is_drawn():
        flash("Reset the draw before removing someone.", "error")
        return redirect(url_for("admin"))
    d.execute("DELETE FROM people WHERE id=?", (pid,))
    d.commit()
    return redirect(url_for("admin"))


@app.post("/admin/people/<int:pid>/edit")
@admin_required
def edit_person(pid):
    """Fix a name, phone number or email. Allowed at any time (doesn't affect the draw)."""
    name = clean_text(request.form.get("name"), 60)
    raw_phone = request.form.get("phone", "")
    phone = clean_phone(raw_phone)
    if not name or (raw_phone.strip() and not phone):
        flash("Needs a name, and a phone number made of digits (add +1 or similar if needed).", "error")
        return redirect(url_for("admin"))
    d = db()
    d.execute("UPDATE people SET name=?, phone=?, email=? WHERE id=?",
              (name, phone, clean_text(request.form.get("email"), 120) or None, pid))
    d.commit()
    flash(f"Saved {name}.", "ok")
    return redirect(url_for("admin"))


@app.post("/admin/settings")
@admin_required
def save_settings():
    set_setting("budget", clean_text(request.form.get("budget"), 40))
    set_setting("event_note", clean_text(request.form.get("event_note"), 300))
    set_setting("avoid_swaps", "1" if request.form.get("avoid_swaps") else "0")
    set_setting("avoid_last", "1" if request.form.get("avoid_last") else "0")
    db().commit()
    flash("Settings saved.", "ok")
    return redirect(url_for("admin"))


@app.post("/admin/exclusions")
@admin_required
def add_exclusion():
    d = db()
    if is_drawn():
        flash("Reset the draw before changing the rules.", "error")
        return redirect(url_for("admin"))
    try:
        a, b = int(request.form["a"]), int(request.form["b"])
    except (KeyError, ValueError):
        flash("Pick two people.", "error")
        return redirect(url_for("admin"))
    if a == b:
        flash("Pick two different people (nobody draws themselves anyway).", "error")
        return redirect(url_for("admin"))
    rows = [(a, b)] + ([(b, a)] if request.form.get("both") else [])
    try:
        for x, y in rows:
            d.execute("INSERT OR IGNORE INTO exclusions(giver_id, receiver_id) VALUES (?, ?)", (x, y))
        d.commit()
    except sqlite3.IntegrityError:
        flash("One of those people no longer exists.", "error")
    return redirect(url_for("admin"))


@app.post("/admin/exclusions/delete")
@admin_required
def delete_exclusion():
    d = db()
    if is_drawn():
        flash("Reset the draw before changing the rules.", "error")
        return redirect(url_for("admin"))
    a, b = request.form.get("a", type=int), request.form.get("b", type=int)
    d.execute("DELETE FROM exclusions WHERE giver_id=? AND receiver_id=?", (a, b))
    if request.form.get("both"):
        d.execute("DELETE FROM exclusions WHERE giver_id=? AND receiver_id=?", (b, a))
    d.commit()
    return redirect(url_for("admin"))


@app.post("/admin/history")
@admin_required
def save_history():
    """Record who gave to whom last year, so those matches can be avoided."""
    d = db()
    last_year = int(get_setting("year")) - 1
    ids = {r["id"] for r in d.execute("SELECT id FROM people")}
    d.execute("DELETE FROM history WHERE year=?", (last_year,))
    for gid in ids:
        raw = request.form.get(f"last_{gid}", "")
        if raw.isdigit() and int(raw) in ids and int(raw) != gid:
            d.execute("INSERT INTO history(year, giver_id, receiver_id) VALUES (?, ?, ?)",
                      (last_year, gid, int(raw)))
    d.commit()
    flash(f"Saved last year's ({last_year}) matches.", "ok")
    return redirect(url_for("admin"))


@app.post("/admin/draw")
@admin_required
def do_draw():
    d = db()
    if is_drawn():
        flash("Names have already been drawn.", "error")
        return redirect(url_for("admin"))
    ids = [r["id"] for r in d.execute("SELECT id FROM people")]
    if len(ids) < 3:
        flash("You need at least 3 people to draw names.", "error")
        return redirect(url_for("admin"))

    base = {i: set() for i in ids}
    for r in d.execute("SELECT giver_id, receiver_id FROM exclusions"):
        base[r["giver_id"]].add(r["receiver_id"])
    last = {i: set() for i in ids}
    year = int(get_setting("year"))
    for r in d.execute("SELECT giver_id, receiver_id FROM history WHERE year=?", (year - 1,)):
        last[r["giver_id"]].add(r["receiver_id"])

    use_last = get_setting("avoid_last") == "1"
    swaps = get_setting("avoid_swaps") == "1"

    def forb(with_last):
        return {i: base[i] | (last[i] if with_last else set()) for i in ids}

    result = find_assignment(ids, forb(use_last), swaps)
    if result is None:
        # Work out which rule is the problem so the message is actionable.
        if use_last and find_assignment(ids, forb(False), swaps):
            msg = "No valid draw exists while avoiding last year's matches. Untick that option in Settings, or loosen the rules."
        elif swaps and find_assignment(ids, forb(use_last), False):
            msg = "No valid draw exists while also avoiding direct swaps. Untick that option in Settings, or loosen the rules."
        else:
            msg = "No valid draw exists with the current rules (too many restrictions for this group). Remove a rule and try again."
        flash(msg, "error")
        return redirect(url_for("admin"))

    for giver, receiver in result.items():
        d.execute("INSERT INTO assignments(giver_id, receiver_id) VALUES (?, ?)", (giver, receiver))
    set_setting("drawn_at", datetime.now().strftime("%b %d, %Y at %I:%M %p"))
    d.commit()
    flash("Names drawn! Send everyone their personal link. You can't see who has whom, so you stay surprised too.", "ok")
    return redirect(url_for("admin"))


@app.post("/admin/reset")
@admin_required
def reset_draw():
    d = db()
    d.execute("DELETE FROM assignments")
    set_setting("drawn_at", "")
    d.commit()
    flash("Draw cleared. You can adjust the rules and draw again.", "ok")
    return redirect(url_for("admin"))


@app.post("/admin/new-year")
@admin_required
def new_year():
    """Archive this year's matches as history and start fresh for next year."""
    d = db()
    if not is_drawn():
        flash("Nothing to archive yet. Draw names first.", "error")
        return redirect(url_for("admin"))
    year = int(get_setting("year"))
    d.execute("INSERT OR REPLACE INTO history(year, giver_id, receiver_id) "
              "SELECT ?, giver_id, receiver_id FROM assignments", (year,))
    d.execute("DELETE FROM assignments")
    d.execute("UPDATE items SET purchased_by=NULL")
    set_setting("year", year + 1)
    set_setting("drawn_at", "")
    d.commit()
    flash(f"Archived {year}. This year's matches will be avoided in the {year + 1} draw.", "ok")
    return redirect(url_for("admin"))


# ---------- participants ----------
def person_or_404(token):
    p = db().execute("SELECT * FROM people WHERE token=?", (token,)).fetchone()
    if not p:
        abort(404)
    return p


@app.route("/p/<token>")
def person_page(token):
    d = db()
    me = person_or_404(token)
    mine = d.execute("SELECT * FROM items WHERE person_id=? ORDER BY id", (me["id"],)).fetchall()
    target = None
    row = d.execute("SELECT receiver_id FROM assignments WHERE giver_id=?", (me["id"],)).fetchone()
    if row:
        target = d.execute("SELECT * FROM people WHERE id=?", (row["receiver_id"],)).fetchone()
    # Everyone's wish lists are public to the rest of the family.
    items_by_person = {}
    for it in d.execute("SELECT * FROM items WHERE person_id!=? ORDER BY id", (me["id"],)):
        items_by_person.setdefault(it["person_id"], []).append(it)
    others = [{"person": p, "gifts": items_by_person.get(p["id"], [])}
              for p in d.execute("SELECT id, name FROM people WHERE id!=? ORDER BY LOWER(name)",
                                 (me["id"],))]
    return render_template(
        "person.html", me=me, token=token, mine=mine, target=target, others=others,
        drawn=is_drawn(), budget=get_setting("budget"), event_note=get_setting("event_note"))


@app.get("/p/<token>/preview")
def preview(token):
    person_or_404(token)
    url = clean_url(request.args.get("url", ""))
    if not url:
        return jsonify(ok=False, error="That doesn't look like a web link.")
    return jsonify(fetch_preview(url))


def _item_fields():
    """Parse the item form. Returns (fields, error)."""
    raw_url = request.form.get("url", "").strip()
    url = clean_url(raw_url)
    if raw_url and not url:
        return None, "That link doesn't look right. It should start with http:// or https://"
    raw_img = request.form.get("image", "").strip()
    image = clean_url(raw_img)
    if raw_img and not image:
        image = None
    return {
        "title": clean_text(request.form.get("title"), 200),
        "url": url,
        "price": clean_text(request.form.get("price"), 30) or None,
        "image": image,
        "note": clean_text(request.form.get("note"), 500) or None,
    }, None


@app.post("/p/<token>/items")
def add_item(token):
    me = person_or_404(token)
    f, err = _item_fields()
    if err:
        flash(err, "error")
        return redirect(url_for("person_page", token=token) + "#mine")
    if f["url"] and not f["title"]:  # no JavaScript / preview skipped: try server-side
        info = fetch_preview(f["url"])
        f["title"] = (info.get("title") or "")[:200]
        f["price"] = f["price"] or info.get("price")
        f["image"] = f["image"] or info.get("image")
    if not f["title"]:
        if f["url"]:
            f["title"] = urlparse(f["url"]).netloc
        else:
            flash("Give the gift a name or paste a link.", "error")
            return redirect(url_for("person_page", token=token) + "#mine")
    d = db()
    d.execute("INSERT INTO items(person_id, title, url, price, image, note) VALUES (?,?,?,?,?,?)",
              (me["id"], f["title"], f["url"], f["price"], f["image"], f["note"]))
    d.commit()
    flash("Added to your wish list.", "ok")
    return redirect(url_for("person_page", token=token) + "#mine")


def _own_item_or_404(me, item_id):
    item = db().execute("SELECT * FROM items WHERE id=? AND person_id=?", (item_id, me["id"])).fetchone()
    if not item:
        abort(404)
    return item


@app.post("/p/<token>/items/<int:item_id>/edit")
def edit_item(token, item_id):
    me = person_or_404(token)
    _own_item_or_404(me, item_id)
    f, err = _item_fields()
    if err or not f["title"]:
        flash(err or "A gift needs a name.", "error")
        return redirect(url_for("person_page", token=token) + "#mine")
    d = db()
    d.execute("UPDATE items SET title=?, url=?, price=?, image=?, note=? WHERE id=?",
              (f["title"], f["url"], f["price"], f["image"], f["note"], item_id))
    d.commit()
    flash("Saved.", "ok")
    return redirect(url_for("person_page", token=token) + "#mine")


@app.post("/p/<token>/items/<int:item_id>/delete")
def delete_item(token, item_id):
    me = person_or_404(token)
    _own_item_or_404(me, item_id)
    d = db()
    d.execute("DELETE FROM items WHERE id=?", (item_id,))
    d.commit()
    return redirect(url_for("person_page", token=token) + "#mine")


@app.post("/p/<token>/purchased/<int:item_id>")
def toggle_purchased(token, item_id):
    """Anyone can mark an item on someone ELSE's list as bought; only they can undo it.

    Others see an anonymous "Already bought" (so nobody learns who buys for whom),
    and the list's owner never sees these marks.
    """
    me = person_or_404(token)
    d = db()
    item = d.execute("SELECT * FROM items WHERE id=? AND person_id!=?", (item_id, me["id"])).fetchone()
    if not item:
        abort(404)
    if item["purchased_by"] is None:
        d.execute("UPDATE items SET purchased_by=? WHERE id=? AND purchased_by IS NULL",
                  (me["id"], item_id))
    elif item["purchased_by"] == me["id"]:
        d.execute("UPDATE items SET purchased_by=NULL WHERE id=?", (item_id,))
    else:
        flash("Someone else already marked that one as bought.", "error")
    d.commit()
    return redirect(url_for("person_page", token=token) + f"#p{item['person_id']}")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
