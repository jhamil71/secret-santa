"""Link previews against a fake store that behaves like real, awkward ones."""
import os
import random
import struct
import sys
import threading
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import preview  # noqa: E402


def make_png(seed=1, size=40):
    """A real PNG of random noise (so it is bigger than the tracking-pixel cutoff)."""
    rnd = random.Random(seed)
    raw = b"".join(b"\x00" + bytes(rnd.randrange(256) for _ in range(size * 3)) for _ in range(size))

    def chunk(kind, data):
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


PNG = make_png()
TINY_GIF = b"GIF89a\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff\x00\x00\x00!\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;"


def page(og_image=None, title="Table Saw DWE7491RS - The Home Depot", extra=""):
    img = f'<meta property="og:image" content="{og_image}">' if og_image else ""
    return (f'<html><head><meta property="og:title" content="{title}">'
            f'<meta property="og:site_name" content="The Home Depot">{img}{extra}</head><body>x</body></html>').encode()


class Store(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body=b"", ctype="text/html", headers=()):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        ua = self.headers.get("User-Agent", "")
        p = self.path
        if p == "/open":                                    # an easy store
            return self.send(200, page("/pics/saw.png"))
        if p == "/bots-only":                               # refuses browsers, serves link-preview crawlers
            if "facebookexternalhit" in ua or "Twitterbot" in ua:
                return self.send(200, page("/pics/saw.png"))
            return self.send(403, b"Access Denied")
        if p == "/blocks-everyone":
            return self.send(403, b"Access Denied")
        if p == "/no-image":
            return self.send(200, page())
        if p == "/jsonld":
            ld = ('<script type="application/ld+json">{"@type":"Product","name":"Mug","image":"/pics/saw.png",'
                  '"offers":{"price":"12.5","priceCurrency":"USD"}}</script>')
            return self.send(200, b"<html><head>" + ld.encode() + b"</head></html>")
        if p == "/image-src":
            return self.send(200, b'<html><head><title>Lamp</title><link rel="image_src" href="/pics/saw.png"></head></html>')
        if p == "/redirect":
            return self.send(302, headers=[("Location", "/open")])
        if p == "/hotlink-guard":                           # picture only loads with a Referer from the store
            return self.send(200, page("/pics/guarded.png"))
        if p == "/pics/saw.png":
            return self.send(200, PNG, "image/png")
        if p == "/pics/guarded.png":
            return self.send(200 if self.headers.get("Referer") else 403, PNG, "image/png")
        if p == "/pics/pixel.gif":
            return self.send(200, TINY_GIF, "image/gif")
        if p == "/pics/fake.png":
            return self.send(200, b"<html>not really a picture, just a long page " * 60, "image/png")
        if p == "/doc.pdf":
            return self.send(200, b"%PDF-1.4", "application/pdf")
        if p == "/direct.png":
            return self.send(200, PNG, "image/png")
        if p == "/tiny-og":
            return self.send(200, page("/pics/pixel.gif"))
        if p == "/fake-og":
            return self.send(200, page("/pics/fake.png"))
        self.send(404, b"nope")


@pytest.fixture(scope="module")
def store():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Store)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


@pytest.fixture(autouse=True)
def allow_local(monkeypatch):
    monkeypatch.setattr(preview, "check_public_url", lambda url: None)  # the fake store is on localhost


def test_easy_store(store):
    r = preview.fetch_preview(store + "/open")
    assert r["ok"] and r["image"] == store + "/pics/saw.png"
    assert r["title"] == "Table Saw DWE7491RS"                         # store name trimmed off the title


def test_store_that_only_serves_link_preview_crawlers(store):
    r = preview.fetch_preview(store + "/bots-only")
    assert r["ok"] and r["image"] == store + "/pics/saw.png" and r["title"]


def test_store_that_blocks_everyone_fails_politely(store):
    r = preview.fetch_preview(store + "/blocks-everyone")
    assert r["ok"] is False and r["image"] is None and "refused" in r["error"]


def test_other_places_a_picture_can_hide(store):
    assert preview.fetch_preview(store + "/jsonld")["image"] == store + "/pics/saw.png"
    assert preview.fetch_preview(store + "/jsonld")["price"] == "$12.50"
    assert preview.fetch_preview(store + "/image-src")["image"] == store + "/pics/saw.png"


def test_redirects_and_missing_picture(store):
    assert preview.fetch_preview(store + "/redirect")["image"] == store + "/pics/saw.png"
    r = preview.fetch_preview(store + "/no-image")
    assert r["ok"] and r["title"] and r["image"] is None


def test_pdf_and_direct_picture_links(store):
    r = preview.fetch_preview(store + "/doc.pdf")
    assert r["title"] == "doc" and r["image"] is None
    assert preview.fetch_preview(store + "/direct.png")["image"] == store + "/direct.png"


def test_copy_of_picture_is_kept_and_verified(store, tmp_path):
    got = preview.cache_image(store + "/pics/saw.png", str(tmp_path))
    assert got and got.startswith("/img/") and got.endswith(".png")
    assert (tmp_path / got.split("/")[-1]).read_bytes() == PNG
    assert preview.cache_image(store + "/pics/saw.png", str(tmp_path)) == got       # same picture, same file
    assert len(list(tmp_path.iterdir())) == 1
    assert preview.cache_image(store + "/pics/pixel.gif", str(tmp_path)) is None   # tracking pixel
    assert preview.cache_image(store + "/pics/fake.png", str(tmp_path)) is None    # says image, isn't one
    assert preview.cache_image(store + "/nope.png", str(tmp_path)) is None
    # a store that only serves its pictures to its own pages
    assert preview.cache_image(store + "/pics/guarded.png", str(tmp_path)) is None
    assert preview.cache_image(store + "/pics/guarded.png", str(tmp_path), referer=store + "/x")


def test_amazon_picture_from_product_code():
    f = preview.amazon_picture
    assert f("https://www.amazon.com/Some-Product-Name/dp/B08N5WRWNW/ref=sr_1_1?keywords=x") == \
        "https://m.media-amazon.com/images/P/B08N5WRWNW.01._SCLZZZZZZZ_.jpg"
    assert f("https://www.amazon.co.uk/gp/product/B08N5WRWNW") .endswith("B08N5WRWNW.01._SCLZZZZZZZ_.jpg")
    assert f("https://www.example.com/dp/B08N5WRWNW") is None                      # only on Amazon


def test_title_cleanup_keeps_real_titles():
    c = preview.clean_title
    url = "https://www.homedepot.com/p/x"
    assert c("DEWALT Table Saw - The Home Depot", "The Home Depot", url) == "DEWALT Table Saw"
    assert c("Spooky Candle | Target", None, "https://www.target.com/p/1") == "Spooky Candle"
    assert c("Pots - Pans - Lids", None, url) == "Pots - Pans - Lids"              # last part isn't the store
    assert c("Just a title", None, url) == "Just a title"


def test_private_addresses_still_refused(monkeypatch):
    monkeypatch.undo()                                                              # restore the real safety check
    for bad in ["http://127.0.0.1/", "http://169.254.169.254/latest/", "http://10.0.0.5/x"]:
        r = preview.fetch_preview(bad)
        assert r["ok"] is False and r["image"] is None
