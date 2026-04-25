"""PokeScrape — Flask backend for Pokemon card product scraping."""
from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass, asdict
from typing import Iterable
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from flask import Flask, Response, jsonify, render_template, request
from flask_cors import CORS

app = Flask(__name__, static_folder="static", template_folder="templates")
CORS(app)


# ---------------------------------------------------------------------------
# Domain config — sets, product types, synonyms
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

# Common alternate spellings / abbreviations seen on retailer sites.
SET_ALIASES: dict[str, list[str]] = {
    "Sword & Shield": ["sword and shield", "sword & shield", "swsh", "sword&shield",
                       "schwert & schild", "schwert und schild", "schwert&schild"],
    "Rebel Clash": ["rebel clash",
                    "clash der rebellen"],
    "Darkness Ablaze": ["darkness ablaze",
                        "flammende finsternis"],
    "Vivid Voltage": ["vivid voltage",
                      "farbenschock"],
    "Battle Styles": ["battle styles",
                      "kampfstile"],
    "Chilling Reign": ["chilling reign",
                       "schaurige herrschaft"],
    "Evolving Skies": ["evolving skies",
                       "drachenwandel"],
    "Fusion Strike": ["fusion strike",
                      "fusionsangriff", "fusions angriff"],
    "Brilliant Stars": ["brilliant stars",
                        "strahlende sterne"],
    "Astral Radiance": ["astral radiance",
                        "astralglanz"],
    "Lost Origin": ["lost origin",
                    "verlorener ursprung"],
    "Silver Tempest": ["silver tempest",
                       "silberne sturmwinde"],
    "Surging Sparks": ["surging sparks",
                       "stürmische funken", "sturmische funken"],
    "Destined Rivals": ["destined rivals",
                        "ewige rivalen"],
}

# Product types we care about, with all the synonyms each maps to.
PRODUCT_TYPES: dict[str, list[str]] = {
    "ETB Case": [
        "etb case",
        "elite trainer box case",
        "case of elite trainer",
        "elite trainer case",
    ],
    "Display Case": [
        "booster box case",
        "display case",
        "case of booster",
        "booster case",
    ],
    "ETB": [
        "etb",
        "elite trainer box",
        "elite-trainer-box",
    ],
    "Booster Box": [
        "booster box",
        "display box",
        "booster display",
        "display",  # last-resort match
    ],
}

# Words that signal we should ignore the listing entirely.
EXCLUDE_KEYWORDS: list[str] = [
    "single pack",
    "blister",
    "bundle",
    "pin collection",
    "tin",
    "build & battle",
    "build and battle",
    "premium collection",
    "v box",
    "ex box",
    "promo",
    "code card",
    "sleeve",
    "binder",
    "playmat",
    "deck box",
    "theme deck",
    "battle deck",
    "starter deck",
    "single",
    "loose pack",
    "1 pack",
    "one pack",
    "3 pack",
    "three pack",
]

PRICE_RE = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)")


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
    """Return canonical product type or None. Order matters: Cases beat singles."""
    for canonical, aliases in PRODUCT_TYPES.items():
        for alias in aliases:
            if alias in title_lc:
                return canonical
    return None


def detect_set(title_lc: str, wanted: set[str]) -> str | None:
    """Return canonical set name if any of the wanted sets match the title."""
    for canonical, aliases in SET_ALIASES.items():
        if canonical not in wanted:
            continue
        for alias in aliases:
            if alias in title_lc:
                return canonical
    return None


def is_excluded(title_lc: str, product_type: str) -> bool:
    """Filter bundles/singles/etc. ETB/Booster Box don't count as 'single pack'."""
    for bad in EXCLUDE_KEYWORDS:
        if bad in title_lc:
            # Don't kill ETBs just because "single" appeared in marketing fluff
            if bad in ("single", "tin") and product_type in ("ETB", "ETB Case"):
                continue
            return True
    return False


# ---------------------------------------------------------------------------
# Scraper
# ---------------------------------------------------------------------------

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def fetch(url: str) -> str:
    resp = requests.get(url, headers=DEFAULT_HEADERS, timeout=20)
    resp.raise_for_status()
    return resp.text


def extract_price(text: str) -> str:
    m = PRICE_RE.search(text)
    return f"${m.group(1)}" if m else ""


def find_listings(html: str, base_url: str, wanted_sets: set[str]) -> list[Listing]:
    """Generic scraper: scan every <a> with text + nearby price for product matches."""
    soup = BeautifulSoup(html, "lxml")
    source = urlparse(base_url).netloc or base_url

    seen: set[tuple[str, str]] = set()
    out: list[Listing] = []

    for a in soup.find_all("a"):
        title = a.get_text(" ", strip=True)
        href = a.get("href") or ""
        if not title or len(title) < 8:
            continue
        title_lc = _norm(title)

        ptype = detect_product_type(title_lc)
        if not ptype:
            continue
        if is_excluded(title_lc, ptype):
            continue
        set_name = detect_set(title_lc, wanted_sets)
        if not set_name:
            continue

        # Look for a price on the anchor or its parent container
        price_text = a.get_text(" ", strip=True)
        price = extract_price(price_text)
        if not price and a.parent:
            price = extract_price(a.parent.get_text(" ", strip=True))

        full_url = urljoin(base_url, href)
        key = (title_lc, full_url)
        if key in seen:
            continue
        seen.add(key)

        out.append(
            Listing(
                title=title,
                price=price,
                url=full_url,
                product_type=ptype,
                set_name=set_name,
                source=source,
            )
        )

    return out


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
        html = fetch(url)
    except requests.RequestException as e:
        return jsonify({"error": f"Could not fetch site: {e}"}), 502

    listings = [
        l for l in find_listings(html, url, wanted_sets) if l.product_type in wanted_types
    ]
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
    app.run(host="127.0.0.1", port=5000, debug=True)
