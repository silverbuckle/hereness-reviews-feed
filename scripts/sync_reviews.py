#!/usr/bin/env python3
"""
HERENESS Reviews Feed Generator
================================

Fetches product reviews from Yotpo API and generates a JSON feed
consumable by Klaviyo Custom Catalog Source.

Flow:
  1. Fetch all active products from Shopify Admin API
  2. For each product, scrape product page to get Yotpo product ID
  3. Fetch reviews from Yotpo Widget API (public, no auth)
  4. Select featured review (4+ stars, longest, most helpful)
  5. Generate JSON feed → docs/reviews.json

Security:
  - Public URL via GitHub Pages
  - Reviewer names are NOT included
  - Only published review text + star score + count
  - robots.txt blocks search engine indexing

Environment variables (via GitHub Actions secrets or .env):
  SHOPIFY_STORE_URL      - e.g. hereness.myshopify.com
  SHOPIFY_ACCESS_TOKEN   - Shopify Admin API read-only token
  YOTPO_APP_KEY          - Yotpo Store ID (public, from widget.js URL)
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import gzip
import urllib.parse
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime, timezone


BASE_DIR = Path(__file__).resolve().parent.parent
DOCS_DIR = BASE_DIR / "docs"
CACHE_DIR = BASE_DIR / "cache"
MAPPING_FILE = CACHE_DIR / "yotpo_product_id_mapping.json"

SHOPIFY_STORE_URL = os.environ.get("SHOPIFY_STORE_URL", "hereness.myshopify.com")
SHOPIFY_ACCESS_TOKEN = os.environ.get("SHOPIFY_ACCESS_TOKEN", "")
YOTPO_APP_KEY = os.environ.get("YOTPO_APP_KEY", "0oKf86FVGfWRgAH35OnetKxc1VlFxnNxuEpNxct2")

MIN_STAR = 4
MIN_CONTENT_LENGTH = 30
MAX_CONTENT_LENGTH = 300


def http_get(url: str, headers: dict | None = None, max_retries: int = 3) -> bytes:
    """Fetch URL with retries and gzip handling."""
    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(url, headers=headers or {})
            req.add_header("User-Agent", "HERENESS-Reviews-Sync/1.0")
            req.add_header("Accept-Encoding", "gzip")
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    data = gzip.decompress(data)
                return data
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = 2 ** attempt
                print(f"  Rate limited, waiting {wait}s...", file=sys.stderr)
                time.sleep(wait)
                continue
            if e.code >= 500 and attempt < max_retries - 1:
                time.sleep(1)
                continue
            raise
        except Exception as e:
            if attempt < max_retries - 1:
                time.sleep(1)
                continue
            raise
    raise RuntimeError(f"Failed after {max_retries} retries: {url}")


def fetch_shopify_products() -> list[dict]:
    """Fetch all active products from Shopify Admin API."""
    if not SHOPIFY_ACCESS_TOKEN:
        raise RuntimeError("SHOPIFY_ACCESS_TOKEN is not set")

    all_products = []
    url = (
        f"https://{SHOPIFY_STORE_URL}/admin/api/2024-01/products.json"
        "?status=active&limit=250&fields=id,title,handle,images,variants,product_type,tags,body_html"
    )
    headers = {"X-Shopify-Access-Token": SHOPIFY_ACCESS_TOKEN}

    while url:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            link = resp.headers.get("Link", "")
            data = json.loads(resp.read())

        all_products.extend(data.get("products", []))
        url = None
        if 'rel="next"' in link:
            for part in link.split(","):
                if 'rel="next"' in part:
                    url = part.split("<")[1].split(">")[0]

    print(f"Shopify: {len(all_products)} active products", file=sys.stderr)
    return all_products


def load_yotpo_id_mapping() -> dict[str, str]:
    """Load cached Shopify handle → Yotpo product ID mapping."""
    if not MAPPING_FILE.exists():
        return {}
    with open(MAPPING_FILE, encoding="utf-8") as f:
        return json.load(f)


def save_yotpo_id_mapping(mapping: dict[str, str]) -> None:
    """Persist mapping to cache."""
    CACHE_DIR.mkdir(exist_ok=True)
    with open(MAPPING_FILE, "w", encoding="utf-8") as f:
        json.dump(mapping, f, ensure_ascii=False, indent=2)


def scrape_yotpo_product_id(handle: str) -> str | None:
    """Scrape the product page to extract Yotpo's internal product ID."""
    try:
        # URL-encode non-ASCII handles
        encoded = urllib.parse.quote(handle, safe="")
        data = http_get(f"https://hereness.jp/products/{encoded}")
        html = data.decode("utf-8", errors="ignore")
        m = re.search(
            r'class="yotpo yotpo-main-widget"[\s\S]{0,300}?data-product-id="(\d+)"',
            html,
        )
        if m:
            return m.group(1)
        m = re.search(r'data-product-id="(\d+)"', html)
        return m.group(1) if m else None
    except Exception as e:
        print(f"  Scrape failed for {handle}: {e}", file=sys.stderr)
        return None


def fetch_yotpo_reviews(yotpo_product_id: str) -> dict | None:
    """Fetch reviews for a Yotpo product via the public widget API."""
    url = (
        f"https://api.yotpo.com/v1/widget/{YOTPO_APP_KEY}"
        f"/products/{yotpo_product_id}/reviews.json?per_page=15&page=1"
    )
    try:
        data = http_get(url)
        parsed = json.loads(data)
    except Exception as e:
        print(f"  Yotpo fetch failed for {yotpo_product_id}: {e}", file=sys.stderr)
        return None

    response = parsed.get("response", {})
    bottomline = response.get("bottomline", {}) or {}
    reviews = response.get("reviews", []) or []

    total = int(response.get("pagination", {}).get("total", 0) or 0)
    avg_score = float(bottomline.get("average_score") or 0)

    if total == 0:
        return None

    # Select best review: score >= 4, appropriate length, prefer longer content
    candidates = [
        r for r in reviews
        if int(r.get("score", 0) or 0) >= MIN_STAR
        and MIN_CONTENT_LENGTH <= len(r.get("content") or "") <= MAX_CONTENT_LENGTH
    ]
    if not candidates:
        # Relax length constraint
        candidates = [
            r for r in reviews
            if int(r.get("score", 0) or 0) >= MIN_STAR
            and len(r.get("content") or "") >= MIN_CONTENT_LENGTH
        ]

    if candidates:
        # Longest content wins (fuller reviews are more useful in emails)
        candidates.sort(key=lambda r: -len(r.get("content") or ""))
        top = candidates[0]
    else:
        top = None

    return {
        "total_reviews": total,
        "average_score": round(avg_score, 1),
        "top_review_text": (top.get("content") or "").strip() if top else None,
        "top_review_score": int(top.get("score") or 0) if top else None,
        "top_review_title": (top.get("title") or "").strip() if top else None,
        # reviewer name is intentionally excluded for privacy
    }


def select_product_shot(images: list[dict]) -> str | None:
    """Prefer product-only shots (filename pattern HERENESS_S* or alt with color names)."""
    if not images:
        return None

    color_keywords = {
        "BLACK", "WHITE", "GRAY", "GREY", "NAVY", "BLUE", "GREEN",
        "OLIVE", "BEIGE", "BROWN", "RED", "YELLOW", "HORIZON",
        "HEATHER", "KHAKI", "INK", "CREAM", "CHARCOAL", "SAND",
        "MOSS", "BORDEAUX", "FOREST", "STONE",
    }

    for img in images:
        src = img.get("src") or ""
        alt = (img.get("alt") or "").upper()
        fname = src.split("/")[-1].split("?")[0]
        if re.search(r"HERENESS_S\d+", fname):
            return src
        if any(c in alt for c in color_keywords):
            return src

    if len(images) >= 2:
        return images[1].get("src")
    return images[0].get("src")


def strip_html(text: str) -> str:
    """Crude HTML tag removal for description field."""
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def build_feed() -> list:
    """
    Assemble the complete feed as a flat JSON array.

    Follows Klaviyo's custom catalog feed schema (verified from klaviyo/devportal):
    - Root is a bare JSON array (not an object)
    - Field names are plain (id, title, link, image_link, description)
    - Custom metadata fields are flat at item level
    - First item MUST contain every field Klaviyo should detect in mapping
    """
    products = fetch_shopify_products()
    mapping = load_yotpo_id_mapping()
    mapping_dirty = False

    items = []
    skipped = 0

    for i, p in enumerate(products, 1):
        handle = p["handle"]
        shopify_id = str(p["id"])
        title = p["title"]

        # Get or discover Yotpo product ID
        yotpo_id = mapping.get(handle)
        if not yotpo_id:
            yotpo_id = scrape_yotpo_product_id(handle)
            if yotpo_id:
                mapping[handle] = yotpo_id
                mapping_dirty = True
                time.sleep(0.3)  # be polite while scraping

        if not yotpo_id:
            print(f"  [{i}/{len(products)}] {title}: no Yotpo ID", file=sys.stderr)
            skipped += 1
            continue

        # Fetch reviews
        reviews = fetch_yotpo_reviews(yotpo_id)
        time.sleep(0.2)  # Yotpo rate limit

        images = p.get("images") or []
        variants = p.get("variants") or []
        price = int(float(variants[0].get("price", 0))) if variants else 0
        body_html = p.get("body_html") or ""
        description = strip_html(body_html)[:500] or title

        # Klaviyo schema: required + optional + custom
        item = {
            # REQUIRED
            "id": shopify_id,
            "title": title,
            "link": f"https://hereness.jp/products/{handle}",
            "image_link": images[0]["src"] if images else "https://hereness.jp/cdn/shop/files/hereness-logo.png",
            "description": description,
            # OPTIONAL
            "price": price,
            "categories": [t.strip() for t in (p.get("tags") or "").split(",") if t.strip()][:10],
            # CUSTOM METADATA (flat, mappable in Klaviyo UI)
            "handle": handle,
            "product_type": p.get("product_type", ""),
            "product_shot_url": select_product_shot(images) or "",
            "average_score": reviews["average_score"] if reviews else 0,
            "total_reviews": reviews["total_reviews"] if reviews else 0,
            "top_review_text": (reviews["top_review_text"] or "") if reviews else "",
            "top_review_score": (reviews["top_review_score"] or 0) if reviews else 0,
            "top_review_title": (reviews["top_review_title"] or "") if reviews else "",
        }

        items.append(item)
        status = f"★{reviews['average_score']} ({reviews['total_reviews']})" if reviews else "no reviews"
        print(f"  [{i}/{len(products)}] {title}: {status}", file=sys.stderr)

    if mapping_dirty:
        save_yotpo_id_mapping(mapping)
        print(f"Mapping cache updated: {len(mapping)} entries", file=sys.stderr)

    # IMPORTANT: Klaviyo detects schema from item[0]. Put an item with reviews
    # first, so all custom fields are picked up during mapping.
    items_with_reviews = [i for i in items if i["total_reviews"] > 0]
    items_without_reviews = [i for i in items if i["total_reviews"] == 0]
    items_with_reviews.sort(key=lambda x: -x["total_reviews"])
    items = items_with_reviews + items_without_reviews

    print(f"\nFeed: {len(items)} items, {skipped} skipped", file=sys.stderr)
    return items


def main() -> int:
    DOCS_DIR.mkdir(exist_ok=True)
    items = build_feed()

    # Klaviyo expects a bare JSON array at the root
    output_path = DOCS_DIR / "reviews.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)

    print(
        f"\nWritten: {output_path} ({output_path.stat().st_size / 1024:.1f} KB)",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
