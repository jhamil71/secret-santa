import os
import random
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
    # 4 people, two couples: only a 4-cycle-ish pattern works
    forbidden = {0: {1}, 1: {0}, 2: {3}, 3: {2}}
    for seed in range(100):
        res = find_assignment([0, 1, 2, 3], forbidden, True, random.Random(seed))
        assert res is not None and sorted(res.values()) == [0, 1, 2, 3]


def test_draw_impossible_returns_none():
    assert find_assignment([0, 1, 2], {0: {1, 2}}) is None
    # 3 people with swaps forbidden and one forced swap
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


# ---------- app flow ----------
@pytest.fixture()
def client():
    app_module.app.config["TESTING"] = True
    c = app_module.app.test_client()
    c.get("/")  # establish session + csrf
    with c.session_transaction() as s:
        s["admin"] = True
    return c


def post(c, path, **data):
    with c.session_transaction() as s:
        data["csrf"] = s["csrf"]
    return c.post(path, data=data, follow_redirects=True)


def test_full_flow(client, monkeypatch):
    names = ["Ann", "Bob", "Cara", "Dan", "Eve", "Fay"]
    for n in names:
        post(client, "/admin/people", name=n, is_me="1" if n == "Ann" else "")
    d = app_module.sqlite3.connect(app_module.DB_PATH)
    d.row_factory = app_module.sqlite3.Row
    ids = {r["name"]: r["id"] for r in d.execute("SELECT * FROM people")}
    tokens = {r["name"]: r["token"] for r in d.execute("SELECT * FROM people")}

    # couples (both ways) + a one-way rule
    post(client, "/admin/exclusions", a=ids["Ann"], b=ids["Bob"], both="1")
    post(client, "/admin/exclusions", a=ids["Cara"], b=ids["Dan"], both="1")
    post(client, "/admin/exclusions", a=ids["Eve"], b=ids["Fay"])
    # last year: Ann->Cara, Cara->Ann
    post(client, "/admin/history", **{f"last_{ids['Ann']}": ids["Cara"], f"last_{ids['Cara']}": ids["Ann"]})

    # wish list with stubbed preview
    monkeypatch.setattr(app_module, "fetch_preview", lambda u: {
        "ok": True, "title": "Cool Lamp", "image": "https://img.example/lamp.jpg", "price": "$45.00", "error": None})
    post(client, f"/p/{tokens['Bob']}/items", url="https://shop.example/lamp", title="", price="", image="", note="warm white")

    rules_ok = 0
    for _ in range(60):
        r = post(client, "/admin/draw")
        assert b"Drawn" in r.data
        rows = d.execute("SELECT giver_id, receiver_id FROM assignments").fetchall()
        pairs = {(r["giver_id"], r["receiver_id"]) for r in rows}
        assert len(pairs) == 6 and len({b for _, b in pairs}) == 6
        for a, b in [("Ann", "Bob"), ("Cara", "Dan"), ("Eve", "Fay")]:
            assert (ids[a], ids[b]) not in pairs
        for a, b in [("Bob", "Ann"), ("Dan", "Cara")]:
            assert (ids[a], ids[b]) not in pairs
        assert (ids["Ann"], ids["Cara"]) not in pairs and (ids["Cara"], ids["Ann"]) not in pairs
        assert all((b, a) not in pairs for a, b in pairs)
        assert all(a != b for a, b in pairs)
        rules_ok += 1
        post(client, "/admin/reset")
    assert rules_ok == 60

    post(client, "/admin/draw")
    # whoever drew Bob sees the lamp with price and image; Bob's own page hides purchase state
    giver_of_bob = d.execute("SELECT giver_id FROM assignments WHERE receiver_id=?", (ids["Bob"],)).fetchone()[0]
    giver_name = next(n for n, i in ids.items() if i == giver_of_bob)
    page = client.get(f"/p/{tokens[giver_name]}").data.decode()
    assert "Cool Lamp" in page and "$45.00" in page and "https://img.example/lamp.jpg" in page
    # admin cannot see matches on the dashboard
    dash = client.get("/admin").data.decode()
    assert "Bob's wish list" not in dash

    # mark bought: only the giver may
    item_id = d.execute("SELECT id FROM items").fetchone()[0]
    other = next(n for n in names if n not in (giver_name,))
    assert client.post(f"/p/{tokens[other]}/purchased/{item_id}",
                       data={"csrf": _csrf(client)}).status_code == 404
    post(client, f"/p/{tokens[giver_name]}/purchased/{item_id}")
    assert d.execute("SELECT purchased FROM items").fetchone()[0] == 1
    bob_page = client.get(f"/p/{tokens['Bob']}").data.decode()
    assert "Bought" not in bob_page


def _csrf(client):
    with client.session_transaction() as s:
        return s["csrf"]


def test_security_basics(client):
    assert client.post("/admin/draw").status_code == 400          # CSRF required
    anon = app_module.app.test_client()
    assert anon.get("/admin").status_code == 302                  # login required
    assert anon.get("/p/not-a-real-token").status_code == 404
    # javascript: links are rejected on wish list items
    p = app_module.sqlite3.connect(app_module.DB_PATH).execute("SELECT token FROM people LIMIT 1").fetchone()
    if p:
        r = post(client, f"/p/{p[0]}/items", url="javascript:alert(1)", title="x")
        assert b"doesn" in r.data
