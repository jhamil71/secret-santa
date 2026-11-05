"""Link previews, the way Google Sheets does it.

Given a product link, find its title, picture and price from the page's own link-preview tags
(Open Graph / Twitter cards / JSON-LD), then keep a copy of the picture on our own server so it
keeps working even if the store changes or blocks hotlinking.

Stores often refuse plain scripts, so each page is tried as an ordinary browser first and then as
the link-preview crawlers that social sites use, which stores allow so their links unfurl.
"""
import hashlib
import ipaddress
import json
import os
import re
import socket
import time
from urllib.parse import unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
AGENTS = [
    BROWSER_UA,
    "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)",
    "Twitterbot/1.0",
]
MAX_PAGE_BYTES = 2_000_000
MAX_IMAGE_BYTES = 4_000_000
MIN_IMAGE_BYTES = 1_200          # smaller than this is a tracking pixel or a "not found" placeholder
MAX_REDIRECTS = 4
# When a store refuses us outright (common from cloud hosts such as Vercel, whose addresses stores treat as
# robots), ask outside link-preview services, which fetch from their own machines. Set LINK_PREVIEW_FALLBACKS=0
# to keep gift links from ever being sent to another company.
MICROLINK_API = "https://api.microlink.io/"
JINA_READER = "https://r.jina.ai/"
SCRAPINGBEE_API = "https://app.scrapingbee.com/api/v1/"   # optional paid-per-use service with real residential addresses
IMAGE_PROXY = "https://wsrv.nl/"     # fetches a picture from its own servers and hands back a resized copy
FALLBACKS_ENABLED = os.environ.get("LINK_PREVIEW_FALLBACKS", "1") != "0"
ON_VERCEL = bool(os.environ.get("VERCEL"))
TOTAL_BUDGET_SECONDS = int(os.environ.get("PREVIEW_BUDGET") or (25 if ON_VERCEL else 30))   # all attempts for one link
IMAGE_BUDGET_SECONDS = 5 if ON_VERCEL else 12
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


def site_name(url):
    """'https://www.homedepot.com/p/...' -> 'homedepot.com'"""
    host = (urlparse(url or "").hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


# ---------------------------------------------------------------- parsing
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


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def clean_title(title, og_site_name, url):
    """'Table Saw DWE7491RS - The Home Depot' -> 'Table Saw DWE7491RS' (drops a trailing store name)."""
    if not title:
        return title
    parts = re.split(r"\s+[\-|–—:]\s+|\s*\|\s*", title)
    if len(parts) > 1:
        tail = _norm(parts[-1])
        host_label = _norm(site_name(url).split(".")[0])
        names = {_norm(og_site_name), host_label}
        if tail and any(n and (tail == n or tail in n or n in tail) for n in names):
            title = title[: title.rfind(parts[-1])].rstrip(" -|–—:").strip() or title
    return title


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
    image = meta("og:image:secure_url", "og:image", "og:image:url", "twitter:image", "twitter:image:src")
    price = meta("product:price:amount", "og:price:amount", "price")
    currency = meta("product:price:currency", "og:price:currency", "priceCurrency")

    if not image:
        link = soup.find("link", rel=lambda r: r and "image_src" in (r if isinstance(r, list) else [r]))
        if link and link.get("href"):
            image = link["href"].strip()

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

    if not image:  # Amazon and a few others: the main product picture, not a social tag
        main = soup.find("img", id="landingImage") or soup.find("img", id="imgBlkFront")
        if main:
            image = main.get("data-old-hires") or main.get("src")
            if not image and main.get("data-a-dynamic-image"):
                try:
                    image = next(iter(json.loads(main["data-a-dynamic-image"])))
                except (ValueError, StopIteration):
                    pass
    if not image:
        image = meta("thumbnail", "image")

    if image:
        image = urljoin(base_url, image.strip())
        if urlparse(image).scheme not in ("http", "https"):
            image = None
    title = clean_title(title, meta("og:site_name"), base_url)
    return {
        "title": (title or "")[:200] or None,
        "image": image,
        "price": format_price(price, currency),
    }


# ---------------------------------------------------------------- fetching
def _open(url, ua, deadline, referer=None, accept="text/html,application/xhtml+xml,*/*;q=0.8"):
    """GET following redirects by hand so every hop is checked. Returns an open streaming response."""
    current = url
    headers = {"User-Agent": ua, "Accept": accept, "Accept-Language": "en-US,en;q=0.9"}
    if referer:
        headers["Referer"] = referer
    for _ in range(MAX_REDIRECTS + 1):
        check_public_url(current)
        left = max(deadline - time.time(), 1.0)
        r = requests.get(current, headers=headers, timeout=(min(4, left), min(7, left)),
                         allow_redirects=False, stream=True)
        if r.is_redirect and r.headers.get("Location"):
            current = urljoin(current, r.headers["Location"])
            r.close()
            continue
        r.final_url = current
        return r
    raise requests.TooManyRedirects("Too many redirects.")


def _sniff_image(data):
    """File extension if the bytes really are a jpeg/png/gif/webp picture, else None."""
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def cache_image(image_url, dest_dir, referer=None, store=None, diag=None):
    """Download a picture, check it really is one, and save it under its content hash.

    Returns '/img/<name>' (served by the app) or None if it can't be fetched or isn't an image.
    `store(name, ext, data)`, if given, keeps the picture somewhere other than dest_dir.
    """
    deadline = time.time() + IMAGE_BUDGET_SECONDS
    for ua in (BROWSER_UA, AGENTS[1]):
        try:
            r = _open(image_url, ua, deadline, referer=referer, accept="image/*,*/*;q=0.5")
            if r.status_code >= 400:
                r.close()
                if diag is not None:
                    diag.append(f"picture download refused ({r.status_code})")
                continue
            data = r.raw.read(MAX_IMAGE_BYTES + 1, decode_content=True)
            r.close()
        except (UnsafeURL, requests.RequestException) as e:
            if diag is not None:
                diag.append(f"picture download failed ({type(e).__name__})")
            continue
        ext = _sniff_image(data)
        if diag is not None and not (ext and MIN_IMAGE_BYTES <= len(data) <= MAX_IMAGE_BYTES):
            diag.append("downloaded file is not a usable picture" if not ext else f"picture size unusable ({len(data)} bytes)")
        if ext and MIN_IMAGE_BYTES <= len(data) <= MAX_IMAGE_BYTES:
            name = f"{hashlib.sha1(data).hexdigest()}.{ext}"
            if store:                                   # e.g. the database, when there is no disk
                store(name, ext, data)
                return f"/img/{name}"
            os.makedirs(dest_dir, exist_ok=True)
            path = os.path.join(dest_dir, name)
            if not os.path.exists(path):
                tmp = path + ".part"
                with open(tmp, "wb") as f:
                    f.write(data)
                os.replace(tmp, path)
            return f"/img/{name}"
    return _cache_via_proxy(image_url, dest_dir, store, diag)


def _cache_via_proxy(image_url, dest_dir, store, diag):
    """The picture's own server refused us (stores often refuse cloud hosts): get it through an image proxy instead."""
    if not FALLBACKS_ENABLED:
        return None
    try:
        check_public_url(image_url)                      # never pass an internal address along
    except UnsafeURL:
        return None
    tries = [("picture proxy", IMAGE_PROXY, {"url": image_url, "w": 700, "we": "", "output": "jpg", "q": 85})]
    if os.environ.get("SCRAPINGBEE_API_KEY"):
        tries.append(("scrapingbee", SCRAPINGBEE_API, {"api_key": os.environ["SCRAPINGBEE_API_KEY"], "url": image_url,
                                                       "premium_proxy": "true", "country_code": "us"}))
    for label, endpoint, params in tries:
        try:
            r = requests.get(endpoint, params=params, headers={"User-Agent": "FamilySecretSanta/1.0"},
                             timeout=(4, 30), stream=True)
            data = r.raw.read(MAX_IMAGE_BYTES + 1, decode_content=True) if r.status_code == 200 else b""
            r.close()
        except requests.RequestException as e:
            if diag is not None:
                diag.append(f"{label} failed ({type(e).__name__})")
            continue
        ext = _sniff_image(data)
        if not (ext and MIN_IMAGE_BYTES <= len(data) <= MAX_IMAGE_BYTES):
            if diag is not None:
                diag.append(f"{label} answered {r.status_code} without a usable picture")
            continue
        name = f"{hashlib.sha1(data).hexdigest()}.{ext}"
        if store:
            store(name, ext, data)
        else:
            os.makedirs(dest_dir, exist_ok=True)
            path = os.path.join(dest_dir, name)
            if not os.path.exists(path):
                with open(path + ".part", "wb") as f:
                    f.write(data)
                os.replace(path + ".part", path)
        if diag is not None:
            diag.append(f"picture saved via {label}")
        return f"/img/{name}"
    return None


def amazon_picture(url):
    """Amazon refuses scripts, but its product pictures live at a predictable address per product code."""
    m = re.search(r"/(?:dp|gp/product|gp/aw/d|exec/obidos/ASIN)/([A-Z0-9]{10})(?:[/?]|$)", urlparse(url).path + "/")
    if m and re.search(r"(^|\.)amazon\.[a-z.]+$", site_name(url)):
        return f"https://m.media-amazon.com/images/P/{m.group(1)}.01._SCLZZZZZZZ_.jpg"
    return None


def _page_meta(url, ua, deadline):
    """One attempt at one page. Returns (metadata dict, problem message or None, HTTP status)."""
    r = _open(url, ua, deadline)
    ctype = (r.headers.get("Content-Type") or "").lower()
    if r.status_code >= 400:
        r.close()
        return {}, f"The site refused the request ({r.status_code}).", r.status_code
    if ctype.startswith("image/"):                      # the link is itself a picture
        r.close()
        return {"image": r.final_url}, None, r.status_code
    if "html" not in ctype and "xml" not in ctype and ctype:
        r.close()                                       # a PDF, say: nothing to preview, but not an error
        name = unquote(urlparse(r.final_url).path.rsplit("/", 1)[-1])
        return {"title": re.sub(r"[-_]+", " ", os.path.splitext(name)[0]).strip()[:200] or None}, None, r.status_code
    body = r.raw.read(MAX_PAGE_BYTES, decode_content=True)
    r.close()
    return parse_metadata(body, r.final_url), None, r.status_code


def _microlink(url, deadline, render=False):
    """Microlink: returns title and picture for a link, fetched from their servers."""
    headers = {"Accept": "application/json", "User-Agent": "FamilySecretSanta/1.0"}
    if os.environ.get("MICROLINK_API_KEY"):
        headers["x-api-key"] = os.environ["MICROLINK_API_KEY"]
    left = max(deadline - time.time(), 1.0)
    params = {"url": url}
    if render:
        params["prerender"] = "true"                 # load the page in a real browser first
    r = requests.get(MICROLINK_API, params=params, headers=headers, timeout=(4, min(20, left)))
    if r.status_code != 200:
        return {}, f"service answered {r.status_code}", r.status_code
    d = (r.json().get("data") or {})
    image = (d.get("image") or {}).get("url")
    return {"title": clean_title(d.get("title"), d.get("publisher"), url), "image": image}, None, r.status_code


def _jina(url, deadline, browser=False):
    """Jina Reader: loads the page in a real browser on their side and hands back the finished HTML."""
    headers = {"X-Return-Format": "html", "Accept": "text/html", "User-Agent": "FamilySecretSanta/1.0"}
    if browser:
        headers["X-Engine"] = "browser"              # slower, but runs the page's scripts like a real browser
    left = max(deadline - time.time(), 1.0)
    r = requests.get(JINA_READER + url, headers=headers, timeout=(4, min(25, left)))
    if r.status_code != 200:
        return {}, f"service answered {r.status_code}", r.status_code
    return parse_metadata(r.text[:MAX_PAGE_BYTES], url), None, r.status_code


BLOCK_TITLES = ("access denied", "just a moment", "attention required", "robot or human", "pardon our interruption",
                "are you a robot", "verify you are human", "security check", "request blocked", "captcha",
                "you have been blocked", "reference #")
BLOCK_IMAGE_HOSTS = ("akamai", "edgesuite", "edgekey", "cloudflare", "captcha", "perimeterx", "datadome", "imperva", "incapsula")


def looks_like_block_page(title, image_url):
    """True when what we got is a store's robot-check page rather than the product (its logo is not a gift picture)."""
    t = (title or "").lower()
    host = site_name(image_url or "")
    return any(b in t for b in BLOCK_TITLES) or any(b in host or b in (image_url or "").lower() for b in BLOCK_IMAGE_HOSTS)


def _scrapingbee(url, deadline):
    """ScrapingBee (only when SCRAPINGBEE_API_KEY is set): fetches through ordinary home internet addresses."""
    left = max(deadline - time.time(), 1.0)
    r = requests.get(SCRAPINGBEE_API, params={"api_key": os.environ["SCRAPINGBEE_API_KEY"], "url": url,
                                              "render_js": "true", "premium_proxy": "true", "country_code": "us"},
                     timeout=(4, min(60, max(left, 20))))
    if r.status_code != 200:
        return {}, f"service answered {r.status_code}", r.status_code
    return parse_metadata(r.text[:MAX_PAGE_BYTES], url), None, r.status_code


def _fallback_services():
    services = [("microlink", _microlink), ("jina reader", _jina),
                ("microlink (browser)", lambda u, d: _microlink(u, d, render=True)),
                ("jina reader (browser)", lambda u, d: _jina(u, d, browser=True))]
    if os.environ.get("SCRAPINGBEE_API_KEY"):
        services.append(("scrapingbee", _scrapingbee))
    return services


def fetch_preview(url):
    """Return {ok, title, image, price, site, error}. Never raises. `image` is the store's own address
    for the picture; call cache_image() to keep a copy."""
    out = {"ok": False, "title": None, "image": None, "price": None, "site": site_name(url), "error": None,
           "attempts": [], "image_source": None}
    deadline = time.time() + TOTAL_BUDGET_SECONDS
    problem = None
    for ua in AGENTS:
        if time.time() >= deadline:
            break
        who = {AGENTS[0]: "browser", AGENTS[1]: "facebook crawler", AGENTS[2]: "twitter crawler"}.get(ua, "other")
        try:
            meta, why, status = _page_meta(url, ua, deadline)
        except UnsafeURL as e:
            out["error"] = str(e)
            return out
        except requests.RequestException as e:
            problem = problem or "Couldn't reach that page."
            out["attempts"].append({"as": who, "status": None, "found_image": False, "note": type(e).__name__})
            continue
        out["attempts"].append({"as": who, "status": status, "found_image": bool(meta.get("image")),
                                "note": why or ("no picture tag in the page" if not meta.get("image") else "ok")})
        problem = why or problem
        for k in ("title", "image", "price"):
            out[k] = out[k] or meta.get(k)
        if out["image"]:
            out["image_source"] = "store"            # the store's own page told us
            break
    if not out["image"] and amazon_picture(url):
        out["image"], out["image_source"] = amazon_picture(url), "store"
    if FALLBACKS_ENABLED and not out["image"]:
        for name, fn in _fallback_services():
            if time.time() >= deadline:
                break
            try:
                meta, why, status = fn(url, deadline)
            except (requests.RequestException, ValueError) as e:
                out["attempts"].append({"as": name, "status": None, "found_image": False, "note": type(e).__name__})
                continue
            out["attempts"].append({"as": name, "status": status, "found_image": bool(meta.get("image")),
                                    "note": why or ("ok" if meta.get("image") else "no picture tag in the page")})
            for k in ("title", "image", "price"):
                out[k] = out[k] or meta.get(k)
            if out["image"]:
                out["image_source"] = "outside service"
                break
    if looks_like_block_page(out["title"], out["image"]):
        out.update(title=None, image=None, price=None, image_source=None)
        out["error"] = "The store showed a robot check instead of the product."
    out["ok"] = bool(out["title"] or out["image"] or out["price"])
    if not out["ok"]:
        out["error"] = out["error"] or problem or "No product details found."
    return out
