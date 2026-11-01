import os
import random
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DATA_DIR"] = tempfile.mkdtemp()
os.environ["ADMIN_PASSWORD"] = "test-pw"

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
    app_module.init_db()
    app_module.app.config["TESTING"] = True
    c = app_module.app.test_client()
    c.get("/")  # establish session + csrf
    with c.session_transaction() as s:
        s["admin"] = True
    return c


def _csrf(c):
    with c.session_transaction() as s:
        return s["csrf"]


def post(c, path, **data):
    data["csrf"] = _csrf(c)
    return c.post(path, data=data, follow_redirects=True)


def rows():
    d = sqlite3.connect(app_module.DB_PATH)
    d.row_factory = sqlite3.Row
    return d


def test_full_flow(client, monkeypatch):
    names = ["Ann", "Bob", "Cara", "Dan", "Eve", "Fay"]
    for i, n in enumerate(names):
        post(client, "/admin/people", name=n, phone=f"(555) 100-000{i}")
    d = rows()
    ids = {r["name"]: r["id"] for r in d.execute("SELECT * FROM people")}
    tokens = {r["name"]: r["token"] for r in d.execute("SELECT * FROM people")}

    # text-link buttons carry the phone number and that person's private link
    dash = client.get("/admin").data.decode()
    assert "sms:5551000000?&amp;body=" in dash
    assert tokens["Ann"] in dash and "Which person are you" not in dash

    # couples (both ways) + a one-way rule + last year's matches
    post(client, "/admin/exclusions", a=ids["Ann"], b=ids["Bob"], both="1")
    post(client, "/admin/exclusions", a=ids["Cara"], b=ids["Dan"], both="1")
    post(client, "/admin/exclusions", a=ids["Eve"], b=ids["Fay"])
    post(client, "/admin/history", **{f"last_{ids['Ann']}": ids["Cara"], f"last_{ids['Cara']}": ids["Ann"]})

    # Bob adds a gift from just a link (preview stubbed)
    monkeypatch.setattr(app_module, "fetch_preview", lambda u: {
        "ok": True, "title": "Cool Lamp", "image": "https://img.example/lamp.jpg", "price": "$45.00", "error": None})
    post(client, f"/p/{tokens['Bob']}/items", url="https://shop.example/lamp", title="", price="", image="", note="warm white")

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

    # every OTHER family member sees Bob's gift, price and picture, whoever they drew
    for n in names:
        page = client.get(f"/p/{tokens[n]}").data.decode()
        if n != "Bob":
            assert "Cool Lamp" in page and "$45.00" in page and "https://img.example/lamp.jpg" in page
            assert ">I bought this</button>" in page
    # dashboard does not reveal the matches
    assert "reveal your person" not in client.get("/admin").data.decode()

    item_id = d.execute("SELECT id FROM items").fetchone()[0]

    def bought_by():
        return rows().execute("SELECT bought_by FROM items WHERE id=?", (item_id,)).fetchone()[0]

    # Bob can't mark his own gift
    assert client.post(f"/p/{tokens['Bob']}/purchased/{item_id}", data={"csrf": _csrf(client)}).status_code == 404
    # Cara marks it bought; it's anonymous to others, hidden from Bob
    post(client, f"/p/{tokens['Cara']}/purchased/{item_id}")
    assert bought_by() == f"p{ids['Cara']}"
    cara_page = client.get(f"/p/{tokens['Cara']}").data.decode()
    dan_page = client.get(f"/p/{tokens['Dan']}").data.decode()
    bob_page = client.get(f"/p/{tokens['Bob']}").data.decode()
    assert "You bought this" in cara_page and "Undo" in cara_page
    assert "Already bought" in dan_page and ">I bought this</button>" not in dan_page
    assert "bought by" not in dan_page.lower()
    assert "Already bought" not in bob_page and "You bought" not in bob_page
    # Dan can't take it over or undo it
    r = post(client, f"/p/{tokens['Dan']}/purchased/{item_id}")
    assert bought_by() == f"p{ids['Cara']}" and b"already marked" in r.data
    # Cara can undo
    post(client, f"/p/{tokens['Cara']}/purchased/{item_id}")
    assert bought_by() is None

    # "Start next year" clears bought marks but keeps the wish lists
    post(client, f"/p/{tokens['Cara']}/purchased/{item_id}")
    post(client, "/admin/new-year")
    assert bought_by() is None
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
        "ok": True, "title": "Cool Lamp", "image": "https://img.example/lamp.jpg", "price": "$45.00", "error": None})
    post(client, f"/p/{p['Bob']['token']}/items", url="https://shop.example/lamp", title="", price="", image="", note="warm white")
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
    for word in ["secret santa", "drawn", "draw ", "reveal", "organizer", "who you're buying"]:
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
    assert "You bought this" in page and "Undo" in page

    # A second outsider sees it as already bought, anonymously, and can't take it over or undo it
    other = _visitor()
    page = other.get(f"/list/{slug}").data.decode()
    assert "Already bought" in page and ">I bought this</button>" not in page and "Undo" not in page
    mark = _bought_by(item_id)
    other.post(f"/list/{slug}/bought/{item_id}", data={"csrf": _csrf(other)})
    assert _bought_by(item_id) == mark

    # A family member (opened their private link) sees the same mark; their private page agrees
    cara = _visitor(f"/p/{p['Cara']['token']}")
    assert "Already bought" in cara.get(f"/list/{slug}").data.decode()
    assert "Already bought" in cara.get(f"/p/{p['Cara']['token']}").data.decode()

    # The outsider undoes theirs; Cara then marks it, and it follows her to her private page
    outsider.post(f"/list/{slug}/bought/{item_id}", data={"csrf": _csrf(outsider)})
    assert _bought_by(item_id) is None
    cara.post(f"/list/{slug}/bought/{item_id}", data={"csrf": _csrf(cara)})
    assert _bought_by(item_id) == f"p{p['Cara']['id']}"
    assert "You bought this" in cara.get(f"/p/{p['Cara']['token']}").data.decode()

    # Bob (opened his own private link) sees his list with NO marks and NO buttons, and can't mark
    bob = _visitor(f"/p/{p['Bob']['token']}")
    page = bob.get(f"/list/{slug}").data.decode()
    assert "Cool Lamp" in page and "your own list" in page
    assert "Already bought" not in page and "You bought" not in page and ">I bought this</button>" not in page
    assert bob.post(f"/list/{slug}/bought/{item_id}", data={"csrf": _csrf(bob)}).status_code == 404
    assert _bought_by(item_id) == f"p{p['Cara']['id']}"
    assert "Already bought" not in bob.get(f"/p/{p['Bob']['token']}").data.decode()

    # Bob's private page offers the share link
    mine = bob.get(f"/p/{p['Bob']['token']}").data.decode()
    assert f"/list/{slug}" in mine and "sms:?&amp;body=" in mine

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

    r = jpost("/admin/settings", budget="$50", event_note="At Grandma's", avoid_swaps="1")
    assert r.status_code == 200 and r.get_json()["ok"] is True
    assert app_module.sqlite3.connect(app_module.DB_PATH).execute(
        "SELECT value FROM settings WHERE key='budget'").fetchone()[0] == "$50"

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
    r = client.post("/admin/settings", data={"csrf": _csrf(client), "budget": "$60"})
    assert r.status_code == 302


def test_security_basics(client):
    assert client.post("/admin/draw").status_code == 400          # CSRF required
    anon = app_module.app.test_client()
    assert anon.get("/admin").status_code == 302                  # login required
    assert anon.get("/p/not-a-real-token").status_code == 404
    post(client, "/admin/people", name="Zed")
    token = rows().execute("SELECT token FROM people").fetchone()[0]
    r = post(client, f"/p/{token}/items", url="javascript:alert(1)", title="x")  # scripts rejected
    assert b"doesn" in r.data
    assert rows().execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0
