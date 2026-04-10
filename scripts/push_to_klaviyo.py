#!/usr/bin/env python3
"""
Push HERENESS reviews feed to Klaviyo Custom Catalog
=====================================================

Reads docs/reviews.json and pushes each item to Klaviyo's Catalog Items API
as integration_type="$custom" items. Uses bulk jobs for efficiency.

Upsert strategy:
  - Attempt POST (create)
  - On 409 Conflict, fall back to PATCH (update)
  - Composite ID format: $custom:::$default:::<external_id>

Usage:
    export KLAVIYO_API_KEY=pk_xxx  # needs catalogs:write scope
    python3 scripts/push_to_klaviyo.py
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
FEED_PATH = BASE_DIR / "docs" / "reviews.json"

KLAVIYO_API_KEY = os.environ.get("KLAVIYO_API_KEY", "")
KLAVIYO_API_BASE = "https://a.klaviyo.com/api"
KLAVIYO_REVISION = "2024-10-15"


def klaviyo_request(method: str, endpoint: str, body: dict | None = None) -> dict:
    """Make a Klaviyo API request."""
    url = f"{KLAVIYO_API_BASE}{endpoint}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Klaviyo-API-Key {KLAVIYO_API_KEY}")
    req.add_header("revision", KLAVIYO_REVISION)
    req.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        payload = e.read().decode("utf-8", errors="ignore")
        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            return {"errors": [{"status": e.code, "detail": payload[:200]}]}


def build_catalog_item(item: dict) -> dict:
    """Convert our feed item format to Klaviyo Catalog Item attributes."""
    return {
        "external_id": str(item["id"]),
        "integration_type": "$custom",
        "catalog_type": "$default",
        "title": item["title"],
        "description": item.get("description") or item["title"],
        "url": item["link"],
        "image_full_url": item["image_link"],
        "image_thumbnail_url": item["image_link"],
        "price": float(item.get("price") or 0),
        "published": True,
        "custom_metadata": {
            "handle": item.get("handle") or "",
            "product_type": item.get("product_type") or "",
            "product_shot_url": item.get("product_shot_url") or "",
            "average_score": item.get("average_score") or 0,
            "total_reviews": item.get("total_reviews") or 0,
            "top_review_text": item.get("top_review_text") or "",
            "top_review_score": item.get("top_review_score") or 0,
            "top_review_title": item.get("top_review_title") or "",
            "categories": ", ".join(item.get("categories") or []),
        },
    }


def create_item(attrs: dict) -> tuple[bool, str]:
    """Create a catalog item. Returns (success, message)."""
    body = {"data": {"type": "catalog-item", "attributes": attrs}}
    resp = klaviyo_request("POST", "/catalog-items/", body)

    if "errors" in resp:
        err = resp["errors"][0]
        status = err.get("status")
        if status == 409 or err.get("code") == "duplicate_resource":
            return False, "409"
        return False, f"{status} {err.get('code')}: {err.get('detail', '')[:80]}"

    return True, resp["data"]["id"]


def update_item(external_id: str, attrs: dict) -> tuple[bool, str]:
    """Update an existing catalog item via PATCH."""
    composite_id = f"$custom:::$default:::{external_id}"
    # For PATCH, remove fields that cannot be updated
    patch_attrs = {k: v for k, v in attrs.items()
                   if k not in ("external_id", "catalog_type", "integration_type")}

    body = {
        "data": {
            "type": "catalog-item",
            "id": composite_id,
            "attributes": patch_attrs,
        }
    }
    resp = klaviyo_request("PATCH", f"/catalog-items/{composite_id}/", body)

    if "errors" in resp:
        err = resp["errors"][0]
        return False, f"{err.get('status')} {err.get('code')}: {err.get('detail', '')[:80]}"

    return True, composite_id


def upsert_item(item: dict) -> tuple[str, str]:
    """Upsert a single catalog item. Returns (action, result)."""
    attrs = build_catalog_item(item)

    # Try create first
    ok, result = create_item(attrs)
    if ok:
        return "created", result

    if result == "409":
        # Item exists, update instead
        ok, result = update_item(attrs["external_id"], attrs)
        if ok:
            return "updated", result
        return "update_error", result

    return "create_error", result


def main() -> int:
    if not KLAVIYO_API_KEY:
        print("ERROR: KLAVIYO_API_KEY is not set", file=sys.stderr)
        return 1

    if not FEED_PATH.exists():
        print(f"ERROR: Feed not found at {FEED_PATH}", file=sys.stderr)
        return 1

    with open(FEED_PATH, encoding="utf-8") as f:
        items = json.load(f)

    print(f"Loaded {len(items)} items from {FEED_PATH.name}", file=sys.stderr)
    print(f"Pushing to Klaviyo Catalog...\n", file=sys.stderr)

    stats = {"created": 0, "updated": 0, "create_error": 0, "update_error": 0}
    errors = []

    for i, item in enumerate(items, 1):
        action, result = upsert_item(item)
        stats[action] = stats.get(action, 0) + 1

        title = item["title"][:40]
        if action in ("created", "updated"):
            marker = "+" if action == "created" else "~"
            print(f"  {marker} [{i}/{len(items)}] {title}", file=sys.stderr)
        else:
            print(f"  ! [{i}/{len(items)}] {title}: {result}", file=sys.stderr)
            errors.append((item["title"], result))

        # Rate limit: 75/sec burst, 700/min steady
        time.sleep(0.1)

    print(f"\n=== Summary ===", file=sys.stderr)
    print(f"  Created: {stats['created']}", file=sys.stderr)
    print(f"  Updated: {stats['updated']}", file=sys.stderr)
    print(f"  Errors:  {stats['create_error'] + stats['update_error']}", file=sys.stderr)

    if errors:
        print(f"\n=== Error details ===", file=sys.stderr)
        for title, err in errors[:10]:
            print(f"  {title}: {err}", file=sys.stderr)

    return 0 if not errors else 2


if __name__ == "__main__":
    sys.exit(main())
