"""PokeScrape — Flask backend for Pokemon card product scraping."""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, asdict
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from flask import Flask, Response, jsonify, render_template, request
from flask_cors import CORS
try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout, Browser
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

app = Flask(__name__, static_folder="static", template_folder="templates")
CORS(app)


# ---------------------------------------------------------------------------
# Domain config
# ---------------------------------------------------------------------------

SETS: list[str] = [
    "Sword & Shield",
    "Rebel Clash",
    "Darkness Ablaze",
    "Vivid Voltage",
    "Battle Styles",
    "Chilling Reign",
    "Evolving Skies",
    "Fusion Strike",
    "Brilliant Stars",
    "Astral Radiance",
    "Lost Origin",
    "Silver Tempest",
    "Surging Sparks",
    "Destined Rivals",
]

SET_ALIASES: dict[str, list[str]] = {
    "Sword & Shield": ["sword and shield", "sword & shield", "swsh", "sword&shield",
                       "schwert & schild", "schwert und schild", "schwert&schild"],
    "Rebel Clash": ["rebel clash", "clash der rebellen"],
    "Darkness Ablaze": ["darkness ablaze", "flammende finsternis"],
    "Vivid Voltage": ["vivid voltage", "farbenschock"],
    "Battle Styles": ["battle styles", "kampfstile"],
    "Chilling Reign": ["chilling reign", "schaurige herrschaft"],
    "Evolving Skies": ["evolving skies", "drachenwandel"],
    "Fusion Strike": ["fusion strike", "fusionsangriff", "fusions angriff"],
    "Brilliant Stars": ["brilliant stars", "strahlende sterne"],
    "Astral Radiance": ["astral radiance", "astralglanz"],
    "Lost Origin": ["lost origin", "verlorener ursprung"],
    "Silver Tempest": ["silver tempest", "silberne sturmwinde"],
    "Surging Sparks": ["surging sparks", "stürmische funken", "sturmische funken"],
    "Destined Rivals": ["destined rivals", "ewige rivalen"],
}

PRODUCT_TYPES: dict[str, list[str]] = {
    "ETB Case": [
        "etb case", "elite trainer box case", "case of elite trainer",
        "elite trainer case", "top-trainer-box case", "top trainer box case",
    ],
    "Display Case": [
        "booster box case", "display case", "case of booster", "booster case",
    ],
    "ETB": [
        "etb", "elite trainer box", "elite-trainer-box",
        "top-trainer-box", "top trainer box",
    ],
    "Booster Box": [
        "booster box", "display box", "booster display",
        "36 booster", "36-booster", "display",
    ],
}

EXCLUDE_KEYWORDS: list[str] = [
    "single pack", "blister", "bundle", "pin collection", "tin",
    "build & battle", "build and battle", "premium collection",
    "v box", "ex box", "promo", "code card", "sleeve", "binder",
    "playmat", "deck box", "theme deck", "battle deck", "starter deck",
    "single", "loose pack", "1 pack", "one pack", "3 pack", "three pack",
    "mini tin", "mini-tin",
]

PRICE_RE = re.compile(r"[\$€£]\s?(\d{1,3}(?:[.,]\d{3})*(?:[.,]\d{2})?)")

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.5",
}


# ---------------------------------------------------------------------------
# Matching helpers
# ---------------------------------------------------------------------------

@dataclass
class Listing:
    title: str
    price: str
    url: str
    product_type: str
    set_name: str
    source: str


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower()).strip()


def detect_product_type(title_lc: str) -> str | None:
    for canonical, aliases in PRODUCT_TYPES.items():
        for alias in aliases:
            if alias in title_lc:
                return canonical
    return None


def detect_set(title_lc: str, wanted: set[str]) -> str | None:
    for canonical, aliases in SET_ALIASES.items():
        if canonical not in wanted:
            continue
        for alias in aliases:
            if alias in title_lc:
                return canonical
    return None


def is_excluded(title_lc: str, product_type: str) -> bool:
    for bad in EXCLUDE_KEYWORDS:
        if bad in title_lc:
            if bad in ("single", "tin") and product_type in ("ETB", "ETB Case"):
                continue
            return True
    return False


def extract_price(text: str) -> str:
    m = PRICE_RE.search(text)
    return m.group(0).strip() if m else ""


def match_listing(title: str, price: str, url: str, source: str,
                  wanted_sets: set[str], wanted_types: set[str]) -> Listing | None:
    title_lc = _norm(title)
    ptype = detect_product_type(title_lc)
    if not ptype or ptype not in wanted_types:
        return None
    if is_excluded(title_lc, ptype):
        return None
    set_name = detect_set(title_lc, wanted_sets)
    if not set_name:
        return None
    return Listing(title=title, price=price, url=url,
                   product_type=ptype, set_name=set_name, source=source)


# ---------------------------------------------------------------------------
# Strategy 1: Shopify JSON API
# ---------------------------------------------------------------------------

def _shopify_products(base_url: str) -> list[dict] | None:
    """Fetch all products from a Shopify store via the JSON API. Returns None if not Shopify."""
    api_url = base_url.rstrip("/") + "/products.json"
    products: list[dict] = []
    page = 1
    while True:
        try:
            r = requests.get(api_url, headers=DEFAULT_HEADERS,
                             params={"limit": 250, "page": page}, timeout=20)
            if not r.ok:
                return None
            data = r.json()
            if "products" not in data:
                return None
            batch = data["products"]
            products.extend(batch)
            if len(batch) < 250:
                break
            page += 1
        except Exception:
            return None
    return products


def scrape_shopify(base_url: str, wanted_sets: set[str], wanted_types: set[str]) -> list[Listing]:
    source = urlparse(base_url).netloc
    products = _shopify_products(base_url)
    if products is None:
        return []

    out: list[Listing] = []
    for p in products:
        title = p.get("title", "")
        handle = p.get("handle", "")
        product_url = base_url.rstrip("/") + f"/products/{handle}"
        # Get the cheapest variant price
        variants = p.get("variants", [])
        price = ""
        if variants:
            try:
                price = "€" + str(min(float(v["price"]) for v in variants if v.get("price")))
            except Exception:
                pass

        listing = match_listing(title, price, product_url, source, wanted_sets, wanted_types)
        if listing:
            out.append(listing)

    return out


# ---------------------------------------------------------------------------
# Strategy 2: Playwright full-browser HTML scrape
# ---------------------------------------------------------------------------

def find_listings_html(html: str, base_url: str, wanted_sets: set[str],
                       wanted_types: set[str]) -> list[Listing]:
    soup = BeautifulSoup(html, "lxml")
    source = urlparse(base_url).netloc or base_url
    seen: set[tuple[str, str]] = set()
    out: list[Listing] = []

    # Collect candidates from <a> tags and nearby headings
    candidates: list[tuple[str, str]] = []  # (title, href)

    for a in soup.find_all("a", href=True):
        title = a.get_text(" ", strip=True)
        if title and len(title) >= 8:
            candidates.append((title, a["href"]))

    # Also pick up product titles in h2/h3/h4 that are inside or adjacent to <a>
    for tag in ("h2", "h3", "h4"):
        for h in soup.find_all(tag):
            text = h.get_text(" ", strip=True)
            if not text or len(text) < 8:
                continue
            # Find href: anchor inside heading OR parent anchor
            a_inner = h.find("a", href=True)
            a_parent = h.find_parent("a")
            href = ""
            if a_inner:
                href = a_inner["href"]
            elif a_parent:
                href = a_parent.get("href", "")
            candidates.append((text, href))

    for title, href in candidates:
        price = ""
        full_url = urljoin(base_url, href) if href else base_url
        key = (_norm(title), full_url)
        if key in seen:
            continue
        seen.add(key)

        listing = match_listing(title, price, full_url, source, wanted_sets, wanted_types)
        if listing:
            # Try to find a price nearby in the soup
            out.append(listing)

    return out


def _pw_get_html(browser: Browser, url: str) -> str:
    ctx = browser.new_context(
        user_agent=DEFAULT_HEADERS["User-Agent"],
        locale="de-DE",
        extra_http_headers={"Accept-Language": "de-DE,de;q=0.9,en;q=0.5"},
    )
    page = ctx.new_page()
    try:
        page.goto(url, wait_until="networkidle", timeout=25000)
        # Scroll to trigger lazy loading
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(2000)
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(1500)
        html = page.content()
    except PWTimeout:
        html = page.content()
    finally:
        ctx.close()
    return html


def _discover_sub_pages(html: str, base_url: str) -> list[str]:
    """Find internal collection/category URLs worth crawling."""
    soup = BeautifulSoup(html, "lxml")
    base_domain = urlparse(base_url).netloc
    keywords = ["display", "booster", "trainer", "etb", "pokemon", "pok",
                 "sammel", "karten", "deutsch", "englisch", "collection",
                 "sets", "products", "shop"]
    found: list[str] = []
    seen: set[str] = {base_url}
    for a in soup.find_all("a", href=True):
        full = urljoin(base_url, a["href"])
        if urlparse(full).netloc != base_domain or full in seen:
            continue
        combined = a["href"].lower() + " " + a.get_text(" ", strip=True).lower()
        if any(kw in combined for kw in keywords):
            found.append(full)
            seen.add(full)
    return found[:15]


def scrape_playwright(base_url: str, wanted_sets: set[str], wanted_types: set[str]) -> list[Listing]:
    if not PLAYWRIGHT_AVAILABLE:
        return []

    all_listings: dict[tuple[str, str], Listing] = {}

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            root_html = _pw_get_html(browser, base_url)

            for l in find_listings_html(root_html, base_url, wanted_sets, wanted_types):
                all_listings[(_norm(l.title), l.url)] = l

            for sub_url in _discover_sub_pages(root_html, base_url):
                try:
                    sub_html = _pw_get_html(browser, sub_url)
                    for l in find_listings_html(sub_html, sub_url, wanted_sets, wanted_types):
                        all_listings[(_norm(l.title), l.url)] = l
                except Exception:
                    continue

    except Exception:
        # Browser binary not available (e.g. Render free tier) — skip silently
        return []

    return list(all_listings.values())


# ---------------------------------------------------------------------------
# Main scrape entry point
# ---------------------------------------------------------------------------

def scrape(url: str, wanted_sets: set[str], wanted_types: set[str]) -> list[Listing]:
    """Try Shopify JSON API first; fall back to Playwright HTML scraping."""
    results = scrape_shopify(url, wanted_sets, wanted_types)
    if results:
        return results
    return scrape_playwright(url, wanted_sets, wanted_types)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index() -> str:
    return render_template("index.html", sets=SETS, product_types=list(PRODUCT_TYPES.keys()))


@app.route("/api/scrape", methods=["POST"])
def api_scrape():
    payload = request.get_json(silent=True) or {}
    url: str = (payload.get("url") or "").strip()
    fmt: str = (payload.get("format") or "json").lower()
    requested_sets: list[str] = payload.get("sets") or SETS
    requested_types: list[str] = payload.get("product_types") or list(PRODUCT_TYPES.keys())

    if not url:
        return jsonify({"error": "Missing url"}), 400
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    wanted_sets = set(requested_sets)
    wanted_types = set(requested_types)

    try:
        listings = scrape(url, wanted_sets, wanted_types)
    except Exception as e:
        return jsonify({"error": f"Could not fetch site: {e}"}), 502

    rows = [asdict(l) for l in listings]

    if fmt == "csv":
        buf = io.StringIO()
        writer = csv.DictWriter(
            buf,
            fieldnames=["title", "price", "product_type", "set_name", "source", "url"],
        )
        writer.writeheader()
        for r in rows:
            writer.writerow({k: r[k] for k in writer.fieldnames})
        return Response(
            buf.getvalue(),
            mimetype="text/csv",
            headers={"Content-Disposition": "attachment; filename=pokescrape.csv"},
        )

    return jsonify({"count": len(rows), "results": rows})


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLY_APP_NAME") is None and os.environ.get("RENDER") is None
    app.run(host="0.0.0.0", port=port, debug=debug)
