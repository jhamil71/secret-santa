import time
import re
import os
import random
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DATA_DIR"] = tempfile.mkdtemp()
os.environ["ADMIN_PASSWORD"] = "test-password"

import app as app_module  # noqa: E402
from draw import find_assignment  # noqa: E402
from preview import UnsafeURL, check_public_url, format_price, parse_metadata  # noqa: E402


# ---------- draw ----------
def test_draw_respects_all_rules_many_times():
    ids = list(range(8))
    couples = [(0, 1), (2, 3), (4, 5)]
    forbidden = {i: set() for i in ids}
    for a, b in couples:
        forbidden[a].add(b)
        forbidden[b].add(a)
    last_year = {i: {(i + 1) % 8} for i in ids}  # everyone gave to the next person
    for i in ids:
        forbidden[i] |= last_year[i]
    for seed in range(300):
        res = find_assignment(ids, forbidden, True, random.Random(seed))
        assert res is not None
        assert sorted(res.values()) == ids
        for g, r in res.items():
            assert g != r and r not in forbidden[g]
            assert res[r] != g  # no swaps


def test_draw_finds_solution_in_tight_case():
    forbidden = {0: {1}, 1: {0}, 2: {3}, 3: {2}}
    for seed in range(100):
        res = find_assignment([0, 1, 2, 3], forbidden, True, random.Random(seed))
        assert res is not None and sorted(res.values()) == [0, 1, 2, 3]


def test_draw_impossible_returns_none():
    assert find_assignment([0, 1, 2], {0: {1, 2}}) is None
    assert find_assignment([0, 1], {}, True) is None


# ---------- previews ----------
def test_parse_og_metadata():
    html = """<html><head><title>Fallback</title>
    <meta property="og:title" content="Cozy Blanket">
    <meta property="og:image" content="/img/blanket.jpg">
    <meta property="product:price:amount" content="39.99">
    <meta property="product:price:currency" content="USD"></head></html>"""
    m = parse_metadata(html, "https://shop.example.com/p/1")
    assert m == {"title": "Cozy Blanket", "image": "https://shop.example.com/img/blanket.jpg", "price": "$39.99"}


def test_parse_json_ld_product():
    html = """<script type="application/ld+json">
    {"@graph":[{"@type":"Product","name":"Espresso Machine","image":["https://x.com/a.png"],
     "offers":{"@type":"Offer","price":"1,299.50","priceCurrency":"EUR"}}]}</script>"""
    m = parse_metadata(html, "https://x.com/")
    assert m["title"] == "Espresso Machine"
    assert m["image"] == "https://x.com/a.png"
    assert m["price"] == "€1,299.50"


def test_price_formats():
    assert format_price("19,99", "EUR") == "€19.99"
    assert format_price("19,99", "SEK") == "19.99 SEK"
    assert format_price("$1,234.5", "USD") == "$1,234.50"
    assert format_price("free") is None


def test_javascript_image_rejected():
    html = '<meta property="og:image" content="javascript:alert(1)">'
    assert parse_metadata(html, "https://x.com/")["image"] is None


@pytest.mark.parametrize("url", ["http://127.0.0.1/", "http://localhost:8000/", "http://169.254.169.254/latest/",
                                 "http://10.0.0.5/", "ftp://example.com/"])
def test_ssrf_blocked(url):
    with pytest.raises(UnsafeURL):
        check_public_url(url)


# ---------- small helpers ----------
def test_clean_phone():
    f = app_module.clean_phone
    assert f("(555) 123-4567") == "5551234567"
    assert f("+1 555 123 4567") == "+15551234567"
    assert f("") is None and f("12") is None and f("call me") is None
    assert f("555-123-4567 ext 9") is None


def test_clean_url_rejects_scripts():
    f = app_module.clean_url
    assert f("javascript:alert(1)") is None
    assert f("https://javascript:alert(1)") is None
    assert f("example.com/x") == "https://example.com/x"


# ---------- app flow ----------
@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "DB_PATH", str(tmp_path / "t.db"))
    fake = None
    if os.environ.get("TEST_TURSO"):
        # Same tests, but the app talks to a (fake) Turso server over HTTP instead of opening the file itself.
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from fake_turso import FakeTurso
        fake = FakeTurso(str(tmp_path / "t.db"))
        monkeypatch.setenv("TURSO_DATABASE_URL", fake.url)
        monkeypatch.setenv("TURSO_AUTH_TOKEN", "secret-token")
    app_module._attempts.clear()
    app_module.init_db()
    app_module.app.config["TESTING"] = True
    c = app_module.app.test_client()
    c.get("/")  # establish session + csrf
    with c.session_transaction() as s:
        s["admin"] = True
        s["admin_at"] = time.time()
    yield c
    if fake:
        fake.stop()


def _csrf(c):
    with c.session_transaction() as s:
        return s["csrf"]


def post(c, path, **data):
    data["csrf"] = _csrf(c)
    return c.post(path, data=data, follow_redirects=True)


def member(phone=None, token=None):
    """A fresh browser signed in as a family member: by typing their phone number, or via their personal link."""
    c = app_module.app.test_client()
    if token:
        c.get(f"/p/{token}", follow_redirects=True)
    else:
        c.get("/")
        c.post("/login", data={"csrf": _csrf(c), "phone": phone}, follow_redirects=True)
    return c


def rows():
    d = sqlite3.connect(app_module.DB_PATH)
    d.row_factory = sqlite3.Row
    return d


def test_full_flow(client, monkeypatch):
    names = ["Ann", "Bob", "Cara", "Dan", "Eve", "Fay"]
    phones = {n: f"(555) 100-000{i}" for i, n in enumerate(names)}
    for n in names:
        post(client, "/admin/people", name=n, phone=phones[n])
    d = rows()
    ids = {r["name"]: r["id"] for r in d.execute("SELECT * FROM people")}
    tokens = {r["name"]: r["token"] for r in d.execute("SELECT * FROM people")}
    slugs = {r["name"]: r["list_slug"] for r in d.execute("SELECT * FROM people")}

    # the organizer page has no text/copy link buttons
    dash = client.get("/admin").data.decode()
    assert "Text link" not in dash and "Copy link" not in dash

    # couples (both ways) + a one-way rule + last year's matches
    post(client, "/admin/exclusions", a=ids["Ann"], b=ids["Bob"], both="1")
    post(client, "/admin/exclusions", a=ids["Cara"], b=ids["Dan"], both="1")
    post(client, "/admin/exclusions", a=ids["Eve"], b=ids["Fay"])
    post(client, "/admin/history", **{f"last_{ids['Ann']}": ids["Cara"], f"last_{ids['Cara']}": ids["Ann"]})

    # Bob signs in with his phone number and adds a gift from just a link (preview stubbed)
    monkeypatch.setattr(app_module, "fetch_preview", lambda u: {
        "ok": True, "title": "Cool Lamp", "image": "https://img.example/lamp.jpg", "image_source": "store", "price": "$45.00", "error": None})
    bob = member(phone=phones["Bob"])
    post(bob, "/mine/items", url="https://shop.example/lamp", title="", price="", image="", note="warm white")

    for _ in range(60):
        r = post(client, "/admin/draw")
        assert b"Drawn" in r.data
        pairs = {(x["giver_id"], x["receiver_id"]) for x in d.execute("SELECT * FROM assignments")}
        assert len(pairs) == 6 and len({b for _, b in pairs}) == 6
        for a, b in [("Ann", "Bob"), ("Bob", "Ann"), ("Cara", "Dan"), ("Dan", "Cara"),
                     ("Eve", "Fay"), ("Ann", "Cara"), ("Cara", "Ann")]:
            assert (ids[a], ids[b]) not in pairs
        assert all((b, a) not in pairs for a, b in pairs) and all(a != b for a, b in pairs)
        post(client, "/admin/reset")
    post(client, "/admin/draw")
    giver_of = {x["giver_id"]: x["receiver_id"] for x in d.execute("SELECT * FROM assignments")}
    name_of = {v: k for k, v in ids.items()}

    for n in names:
        me = member(phone=phones[n])
        home = me.get("/").data.decode()
        assert f"Welcome, {n}" in home
        # the landing page lists everyone, each linking to their public list, except you: you get your editable list
        for other in names:
            if other == n:
                assert f'href="/list/{slugs[other]}"' not in home
            else:
                assert f'href="/list/{slugs[other]}"' in home
        assert 'href="/mine"' in home
        # your drawn person is the one highlighted row (and only that row), linking to their list
        target = name_of[giver_of[ids[n]]]
        assert home.count("other giftee") == 1
        order = re.findall(r'class="pname">([^<(]+?)\s*(?:<|\()', home)
        rest = sorted((x for x in names if x not in (n, target)), key=str.lower)
        assert [x.strip() for x in order] == [target, n] + rest, order
        assert f'class="other giftee">\n        <a class="person" href="/list/{slugs[target]}"' in home
        # everyone but Bob sees Bob's gift on his public list
        if n != "Bob":
            page = me.get(f"/list/{slugs['Bob']}").data.decode()
            assert "Cool Lamp" in page and "$45.00" in page and "https://img.example/lamp.jpg" in page
            assert ">I bought this</button>" in page
        else:
            # ...but Bob can't open his own: he's sent to his editable list
            r = me.get(f"/list/{slugs['Bob']}")
            assert r.status_code == 302 and r.headers["Location"].endswith("/mine")
            mine_page = me.get("/mine").data.decode()
            assert "Cool Lamp" in mine_page and ">I bought this</button>" not in mine_page
    assert "reveal your person" not in client.get("/admin").data.decode()  # organizer can't see matches

    # "Start next year" clears bought marks but keeps the wish lists
    item_id = d.execute("SELECT id FROM items").fetchone()[0]
    cara = member(phone=phones["Cara"])
    post(cara, f"/list/{slugs['Bob']}/bought/{item_id}")
    assert _bought_by(item_id) == f"p{ids['Cara']}"
    post(client, "/admin/new-year")
    assert _bought_by(item_id) is None
    assert rows().execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1


def test_add_and_edit_person_phone(client):
    r = post(client, "/admin/people", name="Zed", phone="not a number")
    assert b"doesn" in r.data and rows().execute("SELECT COUNT(*) FROM people").fetchone()[0] == 0
    post(client, "/admin/people", name="Zed", phone="+1 (555) 222-3333")
    row = rows().execute("SELECT * FROM people").fetchone()
    assert row["phone"] == "+15552223333"
    post(client, f"/admin/people/{row['id']}/edit", name="Zed Z", phone="555 999 8888")
    row = rows().execute("SELECT * FROM people").fetchone()
    assert (row["name"], row["phone"]) == ("Zed Z", "5559998888")


def test_old_database_is_upgraded(tmp_path, monkeypatch):
    path = str(tmp_path / "old.db")
    c = sqlite3.connect(path)
    c.executescript("""
    CREATE TABLE people(id INTEGER PRIMARY KEY, name TEXT NOT NULL, email TEXT, token TEXT NOT NULL UNIQUE);
    CREATE TABLE assignments(giver_id INTEGER PRIMARY KEY, receiver_id INTEGER NOT NULL);
    CREATE TABLE items(id INTEGER PRIMARY KEY, person_id INTEGER NOT NULL, title TEXT NOT NULL, url TEXT,
        price TEXT, image TEXT, note TEXT, purchased INTEGER NOT NULL DEFAULT 0,
        created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
    INSERT INTO people(id,name,token) VALUES (1,'Ann','t1'),(2,'Bob','t2');
    INSERT INTO assignments VALUES (1,2);
    INSERT INTO items(person_id,title,purchased) VALUES (2,'Lamp',1),(2,'Socks',0);
    """)
    c.commit()
    c.close()
    monkeypatch.setattr(app_module, "DB_PATH", path)
    app_module.init_db()
    d = sqlite3.connect(path)
    assert "phone" in {r[1] for r in d.execute("PRAGMA table_info(people)")}
    got = dict(d.execute("SELECT title, bought_by FROM items").fetchall())
    assert got == {"Lamp": "p1", "Socks": None}  # old "bought" mark carried over to the giver
    slugs = [r[0] for r in d.execute("SELECT list_slug FROM people")]
    assert len(slugs) == 2 and all(slugs) and len(set(slugs)) == 2  # public list links backfilled


def _visitor(path_or_none=None):
    """A fresh browser (own cookies). Optionally opens a private page first, like a family member would."""
    c = app_module.app.test_client()
    c.get(path_or_none or "/")
    return c


def _bought_by(item_id):
    return rows().execute("SELECT bought_by FROM items WHERE id=?", (item_id,)).fetchone()[0]


def test_public_wish_list_page(client, monkeypatch):
    for n in ["Ann", "Bob", "Cara"]:
        post(client, "/admin/people", name=n)
    d = rows()
    p = {r["name"]: r for r in d.execute("SELECT * FROM people")}
    monkeypatch.setattr(app_module, "fetch_preview", lambda u: {
        "ok": True, "title": "Cool Lamp", "image": "https://img.example/lamp.jpg", "image_source": "store", "price": "$45.00", "error": None})
    post(member(token=p["Bob"]["token"]), "/mine/items", url="https://shop.example/lamp", title="", price="", image="", note="warm white")
    post(client, "/admin/draw")
    item_id = d.execute("SELECT id FROM items").fetchone()[0]
    slug = p["Bob"]["list_slug"]
    assert slug and slug != p["Bob"]["token"] and len(slug) >= 8  # separate, unguessable link

    # An outsider (no account, never opened a private link) sees the list, with no Secret Santa context
    outsider = _visitor()
    r = outsider.get(f"/list/{slug}")
    page = r.data.decode()
    assert r.status_code == 200
    assert "Cool Lamp" in page and "$45.00" in page and "https://img.example/lamp.jpg" in page
    assert "https://shop.example/lamp" in page and "warm white" in page
    assert 'name="robots" content="noindex' in page and r.headers["X-Robots-Tag"].startswith("noindex")
    assert ">I bought this</button>" in page
    import re
    low = re.sub(r"<style.*?</style>", "", page, flags=re.S).lower()
    # (the page header deliberately matches the home page, so "Secret Santa" appears only there; nothing about the draw does)
    for word in ["drawn", "draw ", "organizer", "who you're buying"]:
        assert word not in low, word
    for person in p.values():
        assert person["token"] not in page
    assert "Ann" not in page and "Cara" not in page
    assert outsider.get("/list/not-a-real-slug").status_code == 404
    assert outsider.get(f"/list/{p['Bob']['token']}").status_code == 404
    assert outsider.get(f"/p/{slug}").status_code == 404

    # The outsider checks it off: it sticks, they can undo it
    outsider.post(f"/list/{slug}/bought/{item_id}", data={"csrf": _csrf(outsider)})
    assert _bought_by(item_id).startswith("v")
    page = outsider.get(f"/list/{slug}").data.decode()
    assert f'data-copy="http://localhost/list/{slug}"' in page and "Copy link to this list" in page
    assert "You bought this" in page and "Undo" in page

    # A second outsider sees it as already bought, anonymously, and can't take it over or undo it
    other = _visitor()
    page = other.get(f"/list/{slug}").data.decode()
    assert "Already bought" in page and ">I bought this</button>" not in page and "Undo" not in page
    mark = _bought_by(item_id)
    other.post(f"/list/{slug}/bought/{item_id}", data={"csrf": _csrf(other)})
    assert _bought_by(item_id) == mark

    # A signed-in family member sees the same mark, with a way back to everyone's lists
    cara = member(token=p["Cara"]["token"])
    cara_page = cara.get(f"/list/{slug}").data.decode()
    assert "Already bought" in cara_page and 'class="mast-link" href="/"' in cara_page
    assert "peek at the checkmarks" not in cara_page
    assert "Sign out" in cara_page and "Sign out" not in outsider.get(f"/list/{slug}").data.decode()

    # The outsider undoes theirs; Cara then marks it as herself
    outsider.post(f"/list/{slug}/bought/{item_id}", data={"csrf": _csrf(outsider)})
    assert _bought_by(item_id) is None
    cara.post(f"/list/{slug}/bought/{item_id}", data={"csrf": _csrf(cara)})
    assert _bought_by(item_id) == f"p{p['Cara']['id']}"
    assert "You bought this" in cara.get(f"/list/{slug}").data.decode()

    # Bob, signed in, can NOT open his own public list (it would show what's been bought for him):
    # he is redirected to his editable list, where no marks or buy buttons exist, and can't mark anything
    bob = member(token=p["Bob"]["token"])
    r = bob.get(f"/list/{slug}")
    assert r.status_code == 302 and r.headers["Location"].endswith("/mine")
    mine = bob.get(f"/list/{slug}", follow_redirects=True).data.decode()
    assert "Cool Lamp" in mine and "your own list" in mine
    assert "Already bought" not in mine and "You bought" not in mine and ">I bought this</button>" not in mine
    assert bob.post(f"/list/{slug}/bought/{item_id}", data={"csrf": _csrf(bob)}).status_code == 404
    assert _bought_by(item_id) == f"p{p['Cara']['id']}"
    # his editable list is simple: every gift open for editing, and no share box or "Your gifts" heading
    assert 'name="title" value="Cool Lamp"' in mine and 'value="warm white"' in mine
    assert "Your gifts" not in mine and "Preview" not in mine and "readonly" not in mine
    # the copyable share link lives on his landing page, as a Copy button
    home = bob.get("/").data.decode()
    assert f'data-copy="http://localhost/list/{slug}"' in home and 'value="http://localhost/list/' not in home
    assert f'data-copy="http://localhost/list/{slug}"' in mine and "Copy link to my list" in mine
    assert "sms:" not in home and "Text link" not in home and "Copy link" in home
    # everyone else's row has a Copy link button for their public list too
    cara_slug = rows().execute("SELECT list_slug FROM people WHERE id=?", (p["Cara"]["id"],)).fetchone()[0]
    assert f'data-copy="http://localhost/list/{cara_slug}"' in home

    # CSRF protection applies to the public toggle too
    assert _visitor().post(f"/list/{slug}/bought/{item_id}").status_code == 400


def test_empty_public_list(client):
    post(client, "/admin/people", name="Zed")
    slug = rows().execute("SELECT list_slug FROM people").fetchone()[0]
    page = app_module.app.test_client().get(f"/list/{slug}").data.decode()
    assert "Nothing on this list yet" in page


JSON = {"X-Requested-With": "fetch", "Accept": "application/json"}


def test_autosave_json_replies(client):
    post(client, "/admin/people", name="Ann")
    post(client, "/admin/people", name="Bob")
    pid = rows().execute("SELECT id FROM people WHERE name='Ann'").fetchone()[0]
    bid = rows().execute("SELECT id FROM people WHERE name='Bob'").fetchone()[0]

    def jpost(path, **data):
        data["csrf"] = _csrf(client)
        return client.post(path, data=data, headers=JSON)

    def setting(key):
        return sqlite3.connect(app_module.DB_PATH).execute(
            "SELECT value FROM settings WHERE key=?", (key,)).fetchone()[0]

    # each checkbox saves on its own, without touching the other
    assert setting("avoid_swaps") == "1" and setting("avoid_last") == "1"
    r = jpost("/admin/settings", field="avoid_last")                      # unticked: box not sent
    assert r.status_code == 200 and r.get_json()["ok"] is True
    assert setting("avoid_last") == "0" and setting("avoid_swaps") == "1"
    jpost("/admin/settings", field="avoid_swaps")
    assert setting("avoid_swaps") == "0"
    jpost("/admin/settings", field="avoid_last", avoid_last="1")
    assert setting("avoid_last") == "1" and setting("avoid_swaps") == "0"
    assert jpost("/admin/settings", field="budget", budget="$5").status_code == 400  # budget/note aren't settings any more

    r = jpost(f"/admin/people/{pid}/edit", name="Annie", phone="555 111 2222")
    assert r.get_json() == {"ok": True, "message": "Saved Annie.", "name": "Annie", "phone": "5551112222"}

    r = jpost(f"/admin/people/{pid}/edit", name="Annie", phone="nope")
    assert r.status_code == 400 and r.get_json()["ok"] is False and "phone" in r.get_json()["error"]
    assert rows().execute("SELECT phone FROM people WHERE id=?", (pid,)).fetchone()[0] == "5551112222"
    assert jpost(f"/admin/people/{pid}/edit", name="", phone="").get_json()["ok"] is False

    r = jpost("/admin/history", **{f"last_{pid}": bid})
    assert r.get_json()["ok"] is True
    assert rows().execute("SELECT receiver_id FROM history WHERE giver_id=?", (pid,)).fetchone()[0] == bid

    # without the JSON headers the same routes still work as ordinary form posts
    r = client.post("/admin/settings", data={"csrf": _csrf(client), "field": "avoid_last", "avoid_last": "1"})
    assert r.status_code == 302


def test_security_basics(client):
    assert client.post("/admin/draw").status_code == 400          # CSRF required
    anon = app_module.app.test_client()
    assert anon.get("/admin").status_code == 302                  # login required
    assert anon.get("/p/not-a-real-token").status_code == 404
    post(client, "/admin/people", name="Zed")
    token = rows().execute("SELECT token FROM people").fetchone()[0]
    zed = member(token=token)
    r = post(zed, "/mine/items", url="javascript:alert(1)", title="x")  # scripts rejected
    assert b"doesn" in r.data
    assert rows().execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0


# ---------- phone sign-in ----------
def test_phone_sign_in(client):
    post(client, "/admin/people", name="Ann", phone="(555) 100-0000")
    post(client, "/admin/people", name="Bob", phone="+1 555 200 0000")

    c = app_module.app.test_client()
    page = c.get("/").data.decode()
    assert "Mobile number" in page and "Welcome," not in page          # signed out: just the sign-in box

    def login(phone, **extra):
        return c.post("/login", data={"csrf": _csrf(c), "phone": phone, **extra}, follow_redirects=True)

    # unknown / too-short numbers don't get in, and say so
    for bad in ["555 999 9999", "123", "", "not a number"]:
        r = login(bad)
        assert b"couldn" in r.data and b"Welcome, " not in r.data
    assert c.get("/mine").status_code == 302                                 # still signed out

    # formatting doesn't matter, including a country code the organizer did or didn't type
    r = login("555-100-0000")
    assert b"Welcome, Ann" in r.data and b"Sign out" in r.data
    assert c.get("/mine").status_code == 200
    c.post("/logout", data={"csrf": _csrf(c)})
    assert c.get("/mine").status_code == 302                                 # signed out again
    assert b"Welcome, Bob" in login("15552000000").data                      # stored as +1..., typed with 1
    c.post("/logout", data={"csrf": _csrf(c)})
    assert b"Welcome, Bob" in login("(555) 200-0000").data                   # stored with +1, typed without


def test_shared_phone_asks_who_you_are(client):
    post(client, "/admin/people", name="Mom", phone="555 300 0000")
    post(client, "/admin/people", name="Dad", phone="555 300 0000")
    ids = {r["name"]: r["id"] for r in rows().execute("SELECT * FROM people")}
    c = app_module.app.test_client()
    c.get("/")
    r = c.post("/login", data={"csrf": _csrf(c), "phone": "5553000000"}, follow_redirects=True)
    assert b"Who are you" in r.data and b"Mom" in r.data and b"Dad" in r.data
    assert c.get("/mine").status_code == 302                                 # not signed in yet
    r = c.post("/login", data={"csrf": _csrf(c), "phone": "5553000000", "person_id": ids["Dad"]},
               follow_redirects=True)
    assert b"Welcome, Dad" in r.data
    # a chosen person must still match the phone number that was typed
    other = app_module.app.test_client()
    other.get("/")
    r = other.post("/login", data={"csrf": _csrf(other), "phone": "5559990000", "person_id": ids["Dad"]},
                   follow_redirects=True)
    assert b"couldn" in r.data


def test_sign_in_is_rate_limited(client):
    post(client, "/admin/people", name="Ann", phone="555 100 0000")
    c = app_module.app.test_client()
    c.get("/")
    for i in range(10):
        c.post("/login", data={"csrf": _csrf(c), "phone": f"555 999 {i:04d}"})
    r = c.post("/login", data={"csrf": _csrf(c), "phone": "555 100 0000"})   # even the right number waits
    assert r.status_code == 429


def test_personal_link_signs_in_and_pages_need_sign_in(client):
    post(client, "/admin/people", name="Ann", phone="555 100 0000")
    token = rows().execute("SELECT token FROM people").fetchone()[0]
    c = app_module.app.test_client()
    r = c.get(f"/p/{token}")
    assert r.status_code == 302 and r.headers["Location"].endswith("/")
    assert b"Welcome, Ann" in c.get("/").data
    # signed-out visitors can't reach or change anyone's editable list
    anon = app_module.app.test_client()
    anon.get("/")
    assert anon.get("/mine").status_code == 302
    assert anon.get("/mine/preview?url=https://example.com").status_code == 302
    assert anon.post("/mine/items", data={"csrf": _csrf(anon), "title": "x"}).status_code == 302
    assert rows().execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0


def test_owner_edits_own_list_only(client):
    post(client, "/admin/people", name="Ann", phone="555 100 0000")
    post(client, "/admin/people", name="Bob", phone="555 100 0001")
    ann, bob = member(phone="555 100 0000"), member(phone="555 100 0001")
    post(ann, "/mine/items", title="Scarf", url="", price="$20", image="", note="")
    item_id = rows().execute("SELECT id FROM items").fetchone()[0]
    # Bob can't edit or delete Ann's gift
    assert bob.post(f"/mine/items/{item_id}/delete", data={"csrf": _csrf(bob)}).status_code == 404
    assert bob.post(f"/mine/items/{item_id}/edit", data={"csrf": _csrf(bob), "title": "Hacked"}).status_code == 404
    post(ann, f"/mine/items/{item_id}/edit", title="Red Scarf", url="", price="$25", image="", note="")
    assert rows().execute("SELECT title, price FROM items").fetchone()[:] == ("Red Scarf", "$25")
    post(ann, f"/mine/items/{item_id}/delete")
    assert rows().execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0


def test_my_list_is_fully_editable_with_autosave(client):
    post(client, "/admin/people", name="Ann", phone="555 100 0000")
    post(client, "/admin/people", name="Bob", phone="555 100 0001")
    ann, bob = member(phone="555 100 0000"), member(phone="555 100 0001")
    post(ann, "/mine/items", title="Scarf", url="https://shop.example/scarf", price="$20", image="", note="red")
    post(ann, "/mine/items", title="Mug", url="", price="", image="", note="")
    ids = [r[0] for r in rows().execute("SELECT id FROM items ORDER BY id")]

    # every existing gift is shown open, with its own editable fields and no Save button needed
    page = ann.get("/mine").data.decode()
    for title in ("Scarf", "Mug"):
        assert f'name="title" value="{title}"' in page
    assert page.count("data-autosave") == 2 and "Your gifts" not in page

    def jpost(c, path, **data):
        return c.post(path, data={"csrf": _csrf(c), **data}, headers=JSON)

    # old entries autosave (JSON); pictures are never typed. Keeping the same link keeps its picture,
    # changing the link drops the old picture and asks the page to go find the new one
    db = rows(); db.execute("UPDATE items SET image='/img/old.png', image_checked=1 WHERE id=?", (ids[0],)); db.commit(); db.close()
    r = jpost(ann, f"/mine/items/{ids[0]}/edit", title="Red Scarf", price="$25", url="https://shop.example/scarf", note="wool")
    assert r.status_code == 200 and r.get_json() == {"ok": True, "message": "Saved.", "image": "/img/old.png", "needs_image": False}
    row = rows().execute("SELECT title, price, note, image FROM items WHERE id=?", (ids[0],)).fetchone()
    assert tuple(row) == ("Red Scarf", "$25", "wool", "/img/old.png")
    r = jpost(ann, f"/mine/items/{ids[0]}/edit", title="Red Scarf", price="$25", url="https://shop.example/scarf2", note="wool")
    assert r.get_json()["image"] is None and r.get_json()["needs_image"] is True
    assert "Picture link" not in ann.get("/mine").data.decode()
    assert "Red Scarf" in ann.get("/mine").data.decode()

    # clearing the name or entering a bad link is refused with a clear message and changes nothing
    r = jpost(ann, f"/mine/items/{ids[0]}/edit", title="", price="$25", url="", image="", note="")
    assert r.status_code == 400 and "needs a name" in r.get_json()["error"]
    r = jpost(ann, f"/mine/items/{ids[0]}/edit", title="Red Scarf", url="javascript:alert(1)", price="", image="", note="")
    assert r.status_code == 400 and "link" in r.get_json()["error"].lower()
    assert rows().execute("SELECT title FROM items WHERE id=?", (ids[0],)).fetchone()[0] == "Red Scarf"

    # nobody else can edit it, and the edit shows on her public list for everyone
    assert jpost(bob, f"/mine/items/{ids[0]}/edit", title="Hacked", url="", price="", image="", note="").status_code == 404
    slug = rows().execute("SELECT list_slug FROM people WHERE name='Ann'").fetchone()[0]
    assert "Red Scarf" in bob.get(f"/list/{slug}").data.decode()


def test_organizer_page_layout(client):
    post(client, "/admin/people", name="Ann", phone="555 100 0000")
    post(client, "/admin/people", name="Bob", phone="555 100 0001")
    page = client.get("/admin").data.decode()
    # the two options live inside their own sections, and the old "Details & options" section is gone
    assert "Details &amp; options" not in page and "Gift budget" not in page and "Note shown to everyone" not in page
    i2, i3, i4 = page.index("2 · Who can"), page.index("3 · Last year"), page.index("4 · Draw names")
    assert i2 < page.index("Avoid direct swaps") < i3 < page.index("Avoid last year") < i4
    assert "5 · Draw" not in page
    # last-year section is live while the option is ticked, greyed out and locked when it is not
    assert 'id="last-year-body" ' in page
    assert 'class="dim"' not in page
    client.post("/admin/settings", data={"csrf": _csrf(client), "field": "avoid_last"})
    off = client.get("/admin").data.decode()
    assert 'id="last-year-body" class="dim" inert' in off
    assert "Pick who each person gave to last year" in off  # still there, just greyed out


def test_draw_ignores_last_year_when_option_is_off(client):
    for n in ("Ann", "Bob", "Cara"):
        post(client, "/admin/people", name=n, phone=f"555 100 00{ord(n[0]) % 100:02d}")
    ids = {r["name"]: r["id"] for r in rows().execute("SELECT * FROM people")}
    # block two of the three one-directional rotations so only Ann>Bob>Cara>Ann is left...
    for a, b in [("Ann", "Cara"), ("Bob", "Ann"), ("Cara", "Bob")]:
        post(client, "/admin/exclusions", a=ids[a], b=ids[b])
    # ...then say that is exactly what happened last year
    post(client, "/admin/history", **{f"last_{ids['Ann']}": ids["Bob"], f"last_{ids['Bob']}": ids["Cara"],
                                      f"last_{ids['Cara']}": ids["Ann"]})
    r = post(client, "/admin/draw")
    assert b"No valid draw" in r.data and rows().execute("SELECT COUNT(*) FROM assignments").fetchone()[0] == 0
    client.post("/admin/settings", data={"csrf": _csrf(client), "field": "avoid_last"})   # turn the option off
    r = post(client, "/admin/draw")
    assert b"Drawn" in r.data and rows().execute("SELECT COUNT(*) FROM assignments").fetchone()[0] == 3


def test_landing_message_is_fixed_text(client):
    post(client, "/admin/people", name="Ann", phone="555 100 0000")
    page = member(phone="555 100 0000").get("/").data.decode()
    assert "Welcome, Ann." in page and "Don’t wait if you see something on your giftee’s wish list" in page
    assert "your giftee may be sharing this wish list with outside folks too!" in page
    assert "Try to spend no more than $100. Have fun!" in page
    assert "Budget:" not in page


def test_organizer_login_hardening(client, monkeypatch):
    # a placeholder or short ADMIN_PASSWORD stops the site from starting
    import importlib, subprocess, sys as _sys
    for bad in ["change-me", "short", "Password"]:
        r = subprocess.run([_sys.executable, "-c", "import app"], capture_output=True, text=True,
                           env={**os.environ, "ADMIN_PASSWORD": bad, "DATA_DIR": tempfile.mkdtemp()},
                           cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        assert r.returncode != 0 and "too weak" in (r.stderr + r.stdout), bad
    # logging in works; the organizer session expires after 12 hours
    c = app_module.app.test_client(); c.get("/admin/login")
    r = c.post("/admin/login", data={"csrf": _csrf(c), "password": "test-password"})
    assert r.status_code == 302 and c.get("/admin").status_code == 200
    with c.session_transaction() as s:
        s["admin_at"] = time.time() - 13 * 3600
    assert c.get("/admin").status_code == 302 and "/admin/login" in c.get("/admin").headers["Location"]
    # wrong passwords get locked out after repeated tries
    bad = app_module.app.test_client(); bad.get("/admin/login")
    for _ in range(10):
        bad.post("/admin/login", data={"csrf": _csrf(bad), "password": "nope-nope"})
    assert bad.post("/admin/login", data={"csrf": _csrf(bad), "password": "test-password"}).status_code == 429


def test_admin_picture_checker(client, monkeypatch):
    # the organizer tool lists gifts with links and re-checks one, reporting what the store said
    p = post(client, "/admin/people", name="Ann", phone="555 100 0000")
    db = rows()
    pid = db.execute("SELECT id FROM people").fetchone()[0]
    db.execute("INSERT INTO items(person_id, title, url) VALUES (?, 'Saw', 'https://store.example/saw')", (pid,))
    db.commit()
    item_id = db.execute("SELECT id FROM items").fetchone()[0]
    page = client.get("/admin/pictures").data.decode()
    assert "Saw" in page and "store.example" in page
    monkeypatch.setattr(app_module, "fetch_preview", lambda url: {
        "image": None, "price": "$9.00", "error": "No product details found.",
        "attempts": [{"as": "browser", "status": 403, "found_image": False, "note": "The site refused the request (403)."}]})
    r = client.post(f"/admin/pictures/{item_id}/check", headers={"X-CSRF": _csrf(client)})
    j = r.get_json()
    assert r.status_code == 200 and j["ok"] is False and j["attempts"][0]["status"] == 403
    assert rows().execute("SELECT price FROM items").fetchone()[0] == "$9.00"
    # visitors who aren't the organizer can't use it
    assert app_module.app.test_client().get("/admin/pictures").status_code == 302


def test_robot_check_pages_never_become_gift_pictures(client, monkeypatch):
    import preview
    # a store's robot-check logo and title are recognised, whichever service returned them
    assert preview.looks_like_block_page("Access Denied", None)
    assert preview.looks_like_block_page("Dewalt Saw", "https://www.akamai.com/logo.png")
    assert not preview.looks_like_block_page("DEWALT Saw", "https://images.thdstatic.com/saw.jpg")

    monkeypatch.setattr(app_module, "cache_image", lambda *a, **k: None)         # the picture can't be copied
    with app_module.app.test_request_context():
        resolve = app_module.resolve_picture
        # the store's picture address is kept when we can't copy it (browsers can usually load it)
        assert resolve({"image": "https://cdn.shop/a.jpg", "image_source": "store"}, "https://shop/p") == "https://cdn.shop/a.jpg"
        # from an outside service and not copyable: still kept as an address, unless it looks like a robot-check logo
        assert resolve({"image": "https://cdn.shop/a.jpg", "image_source": "outside service"}, "https://shop/p") == "https://cdn.shop/a.jpg"
        # a robot-check logo is dropped either way
        assert resolve({"image": "https://www.akamai.com/x.png", "image_source": "store"}, "https://shop/p") is None
        # our own saved copy passes straight through
        assert resolve({"image": "/img/" + "a" * 40 + ".png", "image_source": "outside service"}, "x") == "/img/" + "a" * 40 + ".png"
