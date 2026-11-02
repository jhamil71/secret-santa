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
                   request, send_from_directory, session, url_for)

from draw import find_assignment
from preview import cache_image, fetch_preview, site_name

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(APP_DIR, "data"))
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "santa.db")
IMG_DIR = os.path.join(DATA_DIR, "img")


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
_PLACEHOLDERS = {"change-me", "changeme", "password", "admin", "secret", "santa", "christmas", "12345678"}
if ADMIN_PASSWORD and (len(ADMIN_PASSWORD) < 8 or ADMIN_PASSWORD.lower() in _PLACEHOLDERS):
    raise SystemExit("ADMIN_PASSWORD is too weak or still a placeholder. Use at least 8 characters that "
                     "aren't a common word, or leave it unset and one will be generated for you.")
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
    SESSION_COOKIE_SECURE=(os.environ.get("COOKIE_SECURE") == "1"
                           or (os.environ.get("BASE_URL") or "").startswith("https://")),
    PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 30,
)

if os.environ.get("TRUST_PROXY") == "1":
    # Behind a reverse proxy / port forwarder: use its host and https so links come out right.
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

SCHEMA = """
CREATE TABLE IF NOT EXISTS people(
    id INTEGER PRIMARY KEY, name TEXT NOT NULL, phone TEXT, token TEXT NOT NULL UNIQUE,
    list_slug TEXT);
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
    bought_by TEXT, image_checked INTEGER,
    created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
"""
DEFAULTS = {
    "year": str(datetime.now().year),
    "avoid_swaps": "1", "avoid_last": "1", "drawn_at": "",
}


def init_db():
    with sqlite3.connect(DB_PATH) as c:
        c.executescript(SCHEMA)
        # Upgrade databases created by earlier versions.
        pcols = {r[1] for r in c.execute("PRAGMA table_info(people)")}
        if "phone" not in pcols:
            c.execute("ALTER TABLE people ADD COLUMN phone TEXT")
        if "list_slug" not in pcols:
            c.execute("ALTER TABLE people ADD COLUMN list_slug TEXT")
        for (pid,) in c.execute("SELECT id FROM people WHERE list_slug IS NULL").fetchall():
            c.execute("UPDATE people SET list_slug=? WHERE id=?", (secrets.token_urlsafe(6), pid))
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_people_list_slug ON people(list_slug)")
        icols = {r[1] for r in c.execute("PRAGMA table_info(items)")}
        if "image_checked" not in icols:
            c.execute("ALTER TABLE items ADD COLUMN image_checked INTEGER")
        if "bought_by" not in icols:
            # bought_by is a text key: "p<id>" for a family member, "v<random>" for a visitor's browser.
            c.execute("ALTER TABLE items ADD COLUMN bought_by TEXT")
            if "purchased_by" in icols:
                c.execute("UPDATE items SET bought_by='p'||purchased_by WHERE purchased_by IS NOT NULL")
            elif "purchased" in icols:
                c.execute("UPDATE items SET bought_by=(SELECT 'p'||giver_id FROM assignments "
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


def current_person():
    """The signed-in family member (signed in with their phone number or a personal link), or None."""
    pid = session.get("me_id")
    if not pid:
        return None
    p = db().execute("SELECT * FROM people WHERE id=?", (pid,)).fetchone()
    if not p:
        session.pop("me_id", None)
    return p


def sign_in(person):
    session["me_id"] = person["id"]
    session.permanent = True


def viewer_identity():
    """Who is browsing a public wish list page? Returns (buyer_key, signed_in_person_or_None).

    Signed-in family members mark gifts as themselves (so their marks follow them everywhere).
    Anyone else gets a random per-browser key, so outsiders can check things off too.
    """
    me = current_person()
    if me:
        return f"p{me['id']}", me
    if "vid" not in session:
        session["vid"] = secrets.token_urlsafe(8)
    return f"v{session['vid']}", None


def toggle_bought_mark(d, item, key):
    """Mark an item bought, or undo your own mark. False if someone else holds the mark."""
    if item["bought_by"] is None:
        d.execute("UPDATE items SET bought_by=? WHERE id=? AND bought_by IS NULL", (key, item["id"]))
    elif item["bought_by"] == key:
        d.execute("UPDATE items SET bought_by=NULL WHERE id=?", (item["id"],))
    else:
        return False
    d.commit()
    return True


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


def phone_key(s):
    """Digits only, last 10 if longer, so '+1 (555) 123-4567' matches '5551234567'."""
    digits = re.sub(r"\D", "", s or "")
    return digits[-10:] if len(digits) > 10 else digits


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


def list_link(slug):
    """Public, shareable wish list address. Separate from the private link."""
    return f"{base_url()}{url_for('public_list', slug=slug)}"


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
    return {"csrf": session["csrf"], "viewer": current_person()}


app.jinja_env.filters["site"] = site_name


def keep_picture(image_url, page_url=None):
    """Save a copy of a gift picture on our own server, so it never depends on the store again.
    If the copy can't be made, fall back to the store's own picture address."""
    if not image_url or image_url.startswith("/img/"):
        return image_url or None
    return cache_image(image_url, IMG_DIR, referer=page_url) or image_url


@app.template_filter("site")
def _site_filter(url):
    return site_name(url) if url else ""


@app.get("/img/<name>")
def cached_image(name):
    if not re.fullmatch(r"[0-9a-f]{40}\.(jpg|png|gif|webp)", name):
        abort(404)
    resp = send_from_directory(IMG_DIR, name, max_age=31536000)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


def wants_json():
    return request.headers.get("X-Requested-With") == "fetch" and "application/json" in request.headers.get("Accept", "")


def autosave_reply(ok, message, to="admin", **extra):
    """Autosaving fields get a small JSON answer; plain form posts (no JavaScript) get a flash + redirect."""
    if wants_json():
        payload = {"ok": ok, **extra}
        payload["message" if ok else "error"] = message
        return jsonify(payload), (200 if ok else 400)
    flash(message, "ok" if ok else "error")
    return redirect(url_for(to))


def admin_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not session.get("admin") or time.time() - session.get("admin_at", 0) > ADMIN_SESSION_SECONDS:
            session.pop("admin", None)
            return redirect(url_for("admin_login"))
        return fn(*a, **kw)
    return wrapper


_attempts = {}
ADMIN_SESSION_SECONDS = 12 * 60 * 60     # the organizer signs in again after 12 hours


def too_many_attempts(ip, limit=10):
    now = time.time()
    recent = [t for t in _attempts.get(ip, []) if now - t < 300]
    _attempts[ip] = recent
    return len(recent) >= limit


# ---------- sign in & landing page ----------
def member_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        me = current_person()
        if not me:
            return redirect(url_for("index"))
        return fn(me, *a, **kw)
    return wrapper


@app.route("/")
def index():
    me = current_person()
    if not me:
        return render_template("index.html")
    d = db()
    target = None
    row = d.execute("SELECT receiver_id FROM assignments WHERE giver_id=?", (me["id"],)).fetchone()
    if row:
        target = d.execute("SELECT id, name, list_slug FROM people WHERE id=?", (row["receiver_id"],)).fetchone()
    people = d.execute(
        "SELECT p.id, p.name, p.list_slug, (SELECT COUNT(*) FROM items i WHERE i.person_id=p.id) AS n_items "
        "FROM people p ORDER BY LOWER(p.name)").fetchall()
    # your giftee first, then you, then everyone else alphabetically
    def rank(p):
        return 0 if target and p["id"] == target["id"] else 1 if p["id"] == me["id"] else 2
    people = sorted(people, key=rank)  # stable, so the rest stay alphabetical
    return render_template("home.html", me=me, people=people, target=target, drawn=is_drawn(),
                           year=int(get_setting("year")), share_link=list_link(me["list_slug"]), list_link=list_link)


@app.post("/login")
def login():
    """Pseudo sign-in: just a phone number that the organizer has already entered."""
    ip = f"user:{request.remote_addr or '?'}"
    if too_many_attempts(ip):
        flash("Too many tries. Please wait a few minutes and try again.", "error")
        return render_template("index.html"), 429
    raw = request.form.get("phone", "")
    key = phone_key(raw)
    matches = []
    if len(key) >= 7:
        matches = [p for p in db().execute("SELECT * FROM people")
                   if p["phone"] and phone_key(p["phone"]) == key]
    chosen = request.form.get("person_id", type=int)
    if chosen:
        matches = [p for p in matches if p["id"] == chosen]
    if len(matches) == 1:
        sign_in(matches[0])
        return redirect(url_for("index"))
    if len(matches) > 1:  # one phone shared by several family members: ask who is signing in
        return render_template("index.html", choices=matches, phone=raw)
    _attempts.setdefault(ip, []).append(time.time())
    flash("We couldn't find that number. Check it, or ask the organizer to add you.", "error")
    return render_template("index.html", phone=raw)


@app.post("/logout")
def logout():
    session.pop("me_id", None)
    flash("Signed out.", "ok")
    return redirect(url_for("index"))


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        ip = request.remote_addr or "?"
        if too_many_attempts(ip) or too_many_attempts("admin:any", 40):   # per address, and across all addresses
            flash("Too many attempts. Wait a few minutes and try again.", "error")
            return render_template("admin_login.html"), 429
        pw = request.form.get("password", "")
        if secrets.compare_digest(pw.encode(), ADMIN_PASSWORD.encode()):
            session["admin"] = True
            session["admin_at"] = time.time()
            session.permanent = False
            return redirect(url_for("admin"))
        _attempts.setdefault(ip, []).append(time.time())
        _attempts.setdefault("admin:any", []).append(time.time())
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
        link_for=person_link)


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
    d.execute("INSERT INTO people(name, phone, token, list_slug) VALUES (?, ?, ?, ?)",
              (name, phone, secrets.token_urlsafe(12), secrets.token_urlsafe(6)))
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
    d.execute("UPDATE items SET bought_by=NULL WHERE bought_by=?", (f"p{pid}",))
    d.commit()
    return redirect(url_for("admin"))


@app.post("/admin/people/<int:pid>/edit")
@admin_required
def edit_person(pid):
    """Fix a name or phone number. Allowed at any time (doesn't affect the draw)."""
    name = clean_text(request.form.get("name"), 60)
    raw_phone = request.form.get("phone", "")
    phone = clean_phone(raw_phone)
    if not name:
        return autosave_reply(False, "Every person needs a name.")
    if raw_phone.strip() and not phone:
        return autosave_reply(False, "That phone number doesn't look right. Use digits, adding +1 or similar if needed.")
    d = db()
    d.execute("UPDATE people SET name=?, phone=? WHERE id=?", (name, phone, pid))
    d.commit()
    return autosave_reply(True, f"Saved {name}.", name=name, phone=phone)


@app.post("/admin/settings")
@admin_required
def save_settings():
    """Autosaves one on/off option at a time (each checkbox lives in its own form, in its own section)."""
    field = request.form.get("field")
    if field not in ("avoid_swaps", "avoid_last"):
        return autosave_reply(False, "Unknown setting.")
    set_setting(field, "1" if request.form.get(field) else "0")
    db().commit()
    return autosave_reply(True, "Settings saved.")


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
    return autosave_reply(True, f"Saved last year's ({last_year}) matches.")


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
    d.execute("UPDATE items SET bought_by=NULL")
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
    """A personal link texted by the organizer: signs you in, no phone number needed."""
    sign_in(person_or_404(token))
    return redirect(url_for("index"))


@app.route("/mine")
@member_required
def mine(me):
    items = db().execute("SELECT * FROM items WHERE person_id=? ORDER BY id", (me["id"],)).fetchall()
    return render_template("mine.html", me=me, mine=items, now=int(time.time()), share_link=list_link(me["list_slug"]))


@app.route("/list/<slug>")
def public_list(slug):
    """A person's wish list on its own page, shareable with anyone. No Secret Santa context.

    Anyone can mark gifts as bought so people don't double up; marks are anonymous.
    A signed-in owner is never shown their own public list (it would show what's been bought),
    so they are sent to their editable list instead.
    """
    d = db()
    p = d.execute("SELECT id, name FROM people WHERE list_slug=?", (slug,)).fetchone()
    if not p:
        abort(404)
    key, me = viewer_identity()
    if me and me["id"] == p["id"]:
        flash("That's your own list, and no peeking at what's been bought! Here is where you can edit it.", "ok")
        return redirect(url_for("mine"))
    items = []
    for r in d.execute("SELECT id, title, url, price, image, note, bought_by FROM items "
                       "WHERE person_id=? ORDER BY id", (p["id"],)):
        state = "open" if r["bought_by"] is None else ("mine" if r["bought_by"] == key else "other")
        items.append({**dict(r), "state": state})
    resp = app.make_response(render_template("public_list.html", name=p["name"], slug=slug,
                                             items=items, logged_in=bool(me), share_link=list_link(slug),
                                             year=int(get_setting("year"))))
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.post("/list/<slug>/bought/<int:item_id>")
def public_toggle_bought(slug, item_id):
    d = db()
    p = d.execute("SELECT id FROM people WHERE list_slug=?", (slug,)).fetchone()
    if not p:
        abort(404)
    key, me = viewer_identity()
    if me and me["id"] == p["id"]:
        abort(404)  # nobody checks things off their own list
    item = d.execute("SELECT * FROM items WHERE id=? AND person_id=?", (item_id, p["id"])).fetchone()
    if not item:
        abort(404)
    was_mine = item["bought_by"] == key
    if not toggle_bought_mark(d, item, key):
        flash("Someone else already marked that one as bought.", "error")
    elif was_mine:
        flash("Undone. That gift is available again.", "ok")
    else:
        flash("Marked as bought. Tapped by mistake? Press Undo on that gift.", "ok")
    return redirect(url_for("public_list", slug=slug) + f"#gift{item_id}")


@app.get("/mine/preview")
@member_required
def preview(me):
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


@app.post("/mine/items")
@member_required
def add_item(me):
    f, err = _item_fields()
    if err:
        flash(err, "error")
        return redirect(url_for("mine"))
    if f["url"] and not f["title"]:  # no JavaScript / lookup skipped: do it here
        info = fetch_preview(f["url"])
        f["title"] = (info.get("title") or "")[:200]
        f["price"] = f["price"] or info.get("price")
        f["image"] = f["image"] or info.get("image")
    if not f["title"]:
        if f["url"]:
            f["title"] = site_name(f["url"])
        else:
            flash("Give the gift a name or paste a link.", "error")
            return redirect(url_for("mine"))
    d = db()
    d.execute("INSERT INTO items(person_id, title, url, price, image, note) VALUES (?,?,?,?,?,?)",
              (me["id"], f["title"], f["url"], f["price"],
               keep_picture(f["image"], f["url"]), f["note"]))
    d.commit()
    flash("Added to your wish list.", "ok")
    return redirect(url_for("mine") + "#add")


def _own_item_or_404(me, item_id):
    item = db().execute("SELECT * FROM items WHERE id=? AND person_id=?", (item_id, me["id"])).fetchone()
    if not item:
        abort(404)
    return item


@app.post("/mine/items/<int:item_id>/edit")
@member_required
def edit_item(me, item_id):
    """Edit an existing gift. Autosaves as you type (JSON), or works as a plain form post.
    Nobody types picture links: the picture belongs to the product link and is found automatically."""
    item = _own_item_or_404(me, item_id)
    f, err = _item_fields()
    if err:
        return autosave_reply(False, err, to="mine")
    if not f["title"]:
        return autosave_reply(False, "A gift needs a name.", to="mine")
    link_changed = (f["url"] or None) != (item["url"] or None)
    image, checked = (None, None) if link_changed else (item["image"], item["image_checked"])
    d = db()
    d.execute("UPDATE items SET title=?, url=?, price=?, note=?, image=?, image_checked=? WHERE id=?",
              (f["title"], f["url"], f["price"], f["note"], image, checked, item_id))
    d.commit()
    return autosave_reply(True, "Saved.", to="mine", image=image,
                          needs_image=bool(f["url"] and not image))


@app.post("/mine/items/<int:item_id>/image")
@member_required
def find_item_image(me, item_id):
    """Look up the picture (and price, if missing) for a gift's link. Called in the background."""
    item = _own_item_or_404(me, item_id)
    d = db()
    if not item["url"]:
        return jsonify(ok=False, image=None)
    info = fetch_preview(item["url"])
    image = keep_picture(info.get("image"), item["url"]) if info.get("image") else None
    price = item["price"] or info.get("price")
    d.execute("UPDATE items SET image=COALESCE(?, image), price=?, image_checked=? WHERE id=?",
              (image, price, int(time.time()), item_id))
    d.commit()
    return jsonify(ok=bool(image), image=image or item["image"], price=price, site=site_name(item["url"]))


@app.post("/mine/items/<int:item_id>/delete")
@member_required
def delete_item(me, item_id):
    _own_item_or_404(me, item_id)
    d = db()
    d.execute("DELETE FROM items WHERE id=?", (item_id,))
    d.commit()
    return redirect(url_for("mine"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
