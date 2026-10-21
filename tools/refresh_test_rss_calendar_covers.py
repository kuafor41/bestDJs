#!/usr/bin/env python3
"""Refresh only existing test-RSS cover tags from the public TopDJMixes schedule calendar.

No login or audio URL re-resolution is performed. Titles, dates, descriptions, GUIDs,
MP3 enclosures, order, and unmatched artwork are preserved verbatim.
"""
from __future__ import annotations

import html
import re
import sys
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
import xml.etree.ElementTree as ET

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import build_test_rss as rss

FEED_PATH = ROOT / "public" / "test-rss.xml"
ITEM_RE = re.compile(r"<item>[\\s\\S]*?</item>")
IMAGE_RE = re.compile(r'<itunes:image\\b[^>]*?href="([^"]*)"[^>]*/>')
TITLE_RE = re.compile(r"<title>([\\s\\S]*?)</title>")
LINK_RE = re.compile(r"<link>([\\s\\S]*?)</link>")
ENCLOSURE_RE = re.compile(r"<enclosure\\b")


def decode_element_text(raw: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", raw or "")).strip()


def extract_items(xml_text: str):
    """Build lightweight cover records while retaining the original XML as our output base."""
    blocks = list(ITEM_RE.finditer(xml_text))
    if len(blocks) != rss.TARGET_ITEMS:
        raise RuntimeError(f"Expected {rss.TARGET_ITEMS} existing test items, found {len(blocks)}.")
    items = []
    for block_match in blocks:
        block = block_match.group(0)
        title_match = TITLE_RE.search(block)
        link_match = LINK_RE.search(block)
        cover_match = IMAGE_RE.search(block)
        if not title_match:
            raise RuntimeError("An existing test-RSS item has no title.")
        items.append({
            "title": decode_element_text(title_match.group(1)),
            "page": decode_element_text(link_match.group(1)) if link_match else "",
            "cover": html.unescape(cover_match.group(1)) if cover_match else "",
        })
    return items, blocks


def replace_cover_tags(xml_text: str, items, blocks):
    """Replace only per-item itunes:image href values; leave all other item XML untouched."""
    output = []
    cursor = 0
    changed = 0
    for index, match in enumerate(blocks):
        output.append(xml_text[cursor:match.start()])
        block = match.group(0)
        item = items[index]
        new_cover = item.get("cover") or ""
        old_match = IMAGE_RE.search(block)
        old_cover = html.unescape(old_match.group(1)) if old_match else ""
        if new_cover and new_cover != old_cover:
            tag = f'<itunes:image href="{rss.xml_escape(new_cover)}"/>'
            if old_match:
                block = IMAGE_RE.sub(lambda _m: tag, block, count=1)
            else:
                enclosure = ENCLOSURE_RE.search(block)
                if not enclosure:
                    raise RuntimeError(f"Cannot add cover tag: missing enclosure for {item['title']}.")
                block = block[:enclosure.start()] + tag + "\\n" + block[enclosure.start():]
            changed += 1
        output.append(block)
        cursor = match.end()
    output.append(xml_text[cursor:])
    return "".join(output), changed


def main():
    if not FEED_PATH.exists():
        raise RuntimeError(f"Existing test feed is missing: {FEED_PATH}")
    original = FEED_PATH.read_text(encoding="utf-8")
    # Parse the current XML before scraping, so a malformed source is never overwritten.
    root = ET.fromstring(original)
    entries = root.findall("./channel/item")
    if len(entries) != rss.TARGET_ITEMS:
        raise RuntimeError(f"Expected {rss.TARGET_ITEMS} test items, found {len(entries)}.")

    items, blocks = extract_items(original)
    session = requests.Session()
    session.headers.update(rss.core.HEADERS)
    rss.core.configure_retries(session)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("Playwright is required for the rendered schedule calendar.") from exc

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        context = browser.new_context(user_agent=rss.core.HEADERS["User-Agent"])
        page = context.new_page()
        try:
            # Public schedule page only. This intentionally avoids login attempts and MP3 rebuilds.
            summary = rss.apply_schedule_covers(items, session, page)
        finally:
            browser.close()

    updated, changed = replace_cover_tags(original, items, blocks)
    if changed == 0:
        rss.emit("test_rss_calendar_cover_refresh_no_changes",
                 items=len(items), matched=summary.get("matched", 0),
                 replaced=summary.get("replaced", 0),
                 unmatched=summary.get("unmatched", len(items)),
                 note="Existing test-rss.xml was left untouched.")
        return

    updated = re.sub(
        r"<lastBuildDate>[\\s\\S]*?</lastBuildDate>",
        f"<lastBuildDate>{format_datetime(datetime.now(timezone.utc), usegmt=True)}</lastBuildDate>",
        updated,
        count=1,
    )
    # Validate the new document and preserve the exactly-50-items test contract.
    new_root = ET.fromstring(updated)
    new_items = new_root.findall("./channel/item")
    if len(new_items) != rss.TARGET_ITEMS:
        raise RuntimeError("Updated test RSS no longer contains exactly 50 items.")
    for old_item, new_item in zip(entries, new_items):
        if (old_item.findtext("title") or "") != (new_item.findtext("title") or ""):
            raise RuntimeError("Cover refresh unexpectedly changed episode order or titles.")
        old_enclosure = old_item.find("enclosure")
        new_enclosure = new_item.find("enclosure")
        if old_enclosure is None or new_enclosure is None or old_enclosure.attrib != new_enclosure.attrib:
            raise RuntimeError("Cover refresh unexpectedly changed an MP3 enclosure.")
        if (old_item.findtext("description") or "") != (new_item.findtext("description") or ""):
            raise RuntimeError("Cover refresh unexpectedly changed an episode description.")

    FEED_PATH.write_text(updated, encoding="utf-8")
    rss.emit("test_rss_calendar_cover_refresh_written",
             path=str(FEED_PATH.relative_to(ROOT)),
             items=len(new_items), covers_changed=changed,
             matched=summary.get("matched", 0),
             unmatched=summary.get("unmatched", len(items)),
             production_rss_touched=False,
             audio_urls_changed=False,
             descriptions_changed=False)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        rss.emit("fatal", error=f"{type(exc).__name__}: {exc}")
        sys.exit(1)
