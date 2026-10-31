"""Link previews: fetch a product page and pull out title, image and price."""
import ipaddress
import json
import re
import socket
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}
MAX_BYTES = 1_500_000
MAX_REDIRECTS = 4
CURRENCY_SYMBOLS = {"USD": "$", "EUR": "€", "GBP": "£", "CAD": "CA$", "AUD": "A$"}


class UnsafeURL(Exception):
    pass


def check_public_url(url):
    """Refuse anything that is not a public http(s) host (basic SSRF guard)."""
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise UnsafeURL("Only http(s) links are supported.")
    try:
        infos = socket.getaddrinfo(p.hostname, p.port or (443 if p.scheme == "https" else 80))
    except socket.gaierror:
        raise UnsafeURL("Couldn't find that website.")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified):
            raise UnsafeURL("That address isn't a public website.")


def _nodes(data):
    """Yield every dict inside a JSON-LD blob (handles lists and @graph)."""
    if isinstance(data, list):
        for x in data:
            yield from _nodes(x)
    elif isinstance(data, dict):
        yield data
        if "@graph" in data:
            yield from _nodes(data["@graph"])


def _is_product(node):
    t = node.get("@type")
    return "Product" in (t if isinstance(t, list) else [t])


def _first_image(img):
    if isinstance(img, list):
        return _first_image(img[0]) if img else None
    if isinstance(img, dict):
        return img.get("url") or img.get("contentUrl")
    return img if isinstance(img, str) else None


def _offer_price(offers):
    if isinstance(offers, list):
        offers = offers[0] if offers else None
    if not isinstance(offers, dict):
        return None, None
    spec = offers.get("priceSpecification")
    if isinstance(spec, list):
        spec = spec[0] if spec else None
    price = offers.get("price") or offers.get("lowPrice")
    if price is None and isinstance(spec, dict):
        price = spec.get("price")
    cur = offers.get("priceCurrency") or (spec.get("priceCurrency") if isinstance(spec, dict) else None)
    return price, cur


def format_price(raw, currency=None):
    if raw is None:
        return None
    s = re.sub(r"[^\d.,]", "", str(raw))
    if not s:
        return None
    if "," in s and "." in s:
        s = s.replace(",", "")                       # 1,299.99
    elif "," in s:
        head, _, tail = s.rpartition(",")
        s = f"{head.replace(',', '')}.{tail}" if len(tail) == 2 else s.replace(",", "")
    try:
        amount = float(s)
    except ValueError:
        return None
    text = f"{amount:,.2f}"
    cur = (currency or "").upper()
    if cur in CURRENCY_SYMBOLS:
        return CURRENCY_SYMBOLS[cur] + text
    return f"{text} {cur}".strip()


def parse_metadata(html, base_url):
    soup = BeautifulSoup(html, "html.parser")

    def meta(*names):
        for n in names:
            for attr in ("property", "name", "itemprop"):
                tag = soup.find("meta", attrs={attr: n})
                if tag and tag.get("content", "").strip():
                    return tag["content"].strip()
        return None

    title = meta("og:title", "twitter:title")
    if not title and soup.title and soup.title.string:
        title = soup.title.string.strip()
    image = meta("og:image", "og:image:url", "twitter:image", "twitter:image:src")
    price = meta("product:price:amount", "og:price:amount", "price")
    currency = meta("product:price:currency", "og:price:currency", "priceCurrency")

    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except (ValueError, TypeError):
            continue
        for node in _nodes(data):
            if not _is_product(node):
                continue
            title = title or node.get("name")
            image = image or _first_image(node.get("image"))
            if price is None:
                price, currency = _offer_price(node.get("offers"))
            break

    if image:
        image = urljoin(base_url, image)
        if urlparse(image).scheme not in ("http", "https"):
            image = None
    return {
        "title": (title or "")[:200] or None,
        "image": image,
        "price": format_price(price, currency),
    }


def fetch_preview(url):
    """Return {ok, title, image, price, error}. Never raises."""
    out = {"ok": False, "title": None, "image": None, "price": None, "error": None}
    try:
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            check_public_url(current)
            r = requests.get(current, headers=HEADERS, timeout=(4, 7),
                             allow_redirects=False, stream=True)
            if r.is_redirect and r.headers.get("Location"):
                current = urljoin(current, r.headers["Location"])
                r.close()
                continue
            break
        else:
            out["error"] = "Too many redirects."
            return out
        if r.status_code >= 400:
            out["error"] = f"The site refused the request ({r.status_code}). Add details by hand."
            return out
        body = r.raw.read(MAX_BYTES, decode_content=True)
        r.close()
        meta = parse_metadata(body, current)
    except UnsafeURL as e:
        out["error"] = str(e)
        return out
    except requests.RequestException:
        out["error"] = "Couldn't reach that page. Add details by hand."
        return out
    out.update(meta)
    out["ok"] = bool(meta["title"] or meta["image"] or meta["price"])
    if not out["ok"]:
        out["error"] = "No product details found. Add them by hand."
    return out
