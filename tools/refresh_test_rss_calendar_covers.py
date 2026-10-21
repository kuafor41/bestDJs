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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import build_test_rss as rss

FEED_PATH = ROOT / "public" / "test-rss.xml"
ITEM_RE = re.compile(r"<item>[\\s\\S]*?</item>")
IMAGE_RE = re.compile(r'<itunes:image\\b[^>]*?href="([^"]*)"[^>]*/>')
TITLE_RE = re.compile(r"<title>([\\s\\S]*?)</title>")
LINK_RE = re.compile(r"<link>([\\s\\S]*?)</link>")
ENCLOSURE_RE = re.compile(r"<enclosure\\b")
MONTH_RE = re.compile(r"\\b(January|February|March|April|May|June|July|August|September|October|November|December)\\s*,?\\s*(20\\d{2})\\b", re.I)


def decode_element_text(raw: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", raw or "")).strip()


def extract_items(xml_text: str):
    """Build cover records but retain the original XML as the output base."""
    blocks = list(ITEM_RE.finditer(xml_text))
    if len(blocks) != rss.TARGET_ITEMS:
        raise RuntimeError(f"Expected {rss.TARGET_ITEMS} existing test items, found {len(blocks)}.")
    items = []
    for match in blocks:
        block = match.group(0)
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


def current_month(snapshot):
    for value in snapshot.get("headings", []):
        hit = MONTH_RE.search(value)
        if hit:
            return f"{hit.group(1).title()}, {hit.group(2)}"
    hit = MONTH_RE.search(snapshot.get("body", ""))
    return f"{hit.group(1).title()}, {hit.group(2)}" if hit else ""


def next_month_control(snapshot):
    """Prefer accessible month navigation; avoid previous/back controls."""
    ranked = []
    for row in snapshot.get("controls", []):
        if row.get("disabled"):
            continue
        label = " ".join(str(row.get(k, "")) for k in (
            "aria", "title", "cls", "text", "href", "parentClass"
        )).lower()
        if re.search(r"\\b(previous|prev|back|left)\\b", label):
            continue
        score = 0
        if re.search(r"\\b(next month|next|forward|following month)\\b", label):
            score += 100
        if re.search(r"fc-next|next-month|calendar-next|month-next", label):
            score += 120
        if row.get("text", "").strip() in (">", "›", "»", "❯", "→", "⟶"):
            score += 12
        if re.search(r"calendar|month|schedule|event|fc-", label):
            score += 8
        if row.get("href") and re.search(r"month|calendar|schedule|tribe-bar-date|[?&]m=", row["href"], re.I):
            score += 10
        if score:
            ranked.append((score, row))
    ranked.sort(key=lambda entry: entry[0], reverse=True)
    if not ranked:
        return None
    if len(ranked) > 1 and ranked[0][0] == ranked[1][0] and ranked[0][1]["index"] != ranked[1][1]["index"]:
        return None
    return ranked[0][1]


def discover_calendar_covers(session, items, page):
    """Traverse visible month views and collect unique square covers from the schedule DOM."""
    try:
        page.goto(rss.SCHEDULE_URL, wait_until="domcontentloaded", timeout=45000)
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        page.wait_for_timeout(800)
    except Exception as exc:
        rss.emit("schedule_calendar_unavailable", error=type(exc).__name__)
        return {}

    catalog = {}
    cover_rows = {}
    dimension_cache = {}
    seen_signatures = set()
    seen_square_urls = set()
    scanned_months = []
    item_keys = [
        {"key": rss._compact_cover_label(item.get("title", "")), "title": item.get("title", "")}
        for item in items
    ]

    def add_link(href, title, base_url):
        slug = rss._schedule_slug(href, base_url)
        title = (title or "").strip()
        key = rss._compact_cover_label(title)
        if slug and key and len(key) >= 5:
            catalog[(slug, key)] = {"slug": slug, "title": title, "key": key, "url": href}

    max_months = 18
    for month_index in range(max_months):
        try:
            snap = rss._schedule_calendar_snapshot(page)
        except Exception as exc:
            rss.emit("schedule_calendar_snapshot_error", month_index=month_index, error=type(exc).__name__)
            break

        month = current_month(snap) or f"Month view {month_index + 1}"
        for link in snap.get("links", []):
            add_link(link.get("href", ""), link.get("text") or link.get("title", ""), snap.get("url") or rss.SCHEDULE_URL)

        signature = (
            month,
            tuple(sorted(src for img in snap.get("images", []) for src in img.get("sources", []) if src)),
        )
        if signature in seen_signatures:
            rss.emit("schedule_calendar_navigation_stopped", reason="repeated_month_content", month=month)
            break
        seen_signatures.add(signature)
        scanned_months.append(month)

        new_urls = set()
        for img in snap.get("images", []):
            labels = [img.get("alt", ""), img.get("title", "")]
            for context in img.get("contexts", []):
                text = context.get("text", "")
                if text and len(text) <= 260 and len(context.get("links", [])) <= 6:
                    labels.append(text)
                for link in context.get("links", []):
                    labels.extend([link.get("text", ""), link.get("title", "")])
                    if link.get("href"):
                        labels.append(urlparse(link["href"]).path.rsplit("/", 1)[-1])
            normalized = [rss._compact_cover_label(value) for value in labels
                          if value and len(rss._compact_cover_label(value)) >= 5]

            for raw in img.get("sources", []):
                url = urljoin(snap.get("url") or rss.SCHEDULE_URL, html.unescape(str(raw or "").strip()))
                parsed = urlparse(url)
                if parsed.scheme not in ("http", "https") or not re.search(r"\\.(?:jpe?g|png|webp|avif|gif)$", parsed.path, re.I):
                    continue
                filename_key = rss._compact_cover_label(parsed.path.rsplit("/", 1)[-1])
                match_labels = normalized + ([filename_key] if filename_key else [])
                if not match_labels:
                    continue

                show_matches = [
                    entry for entry in catalog.values()
                    if entry["key"] and any(
                        entry["key"] in label or (len(label) >= 12 and label in entry["key"])
                        for label in match_labels
                    )
                ]
                episode_matches = []
                for item in item_keys:
                    key = item["key"]
                    if len(key) >= 9 and any(key in label for label in match_labels):
                        episode_matches.append(item)

                if not show_matches and not episode_matches:
                    continue
                score = rss._square_variant_score(session, url, img.get("width", 0),
                                                  img.get("height", 0), dimension_cache)
                if score is None:
                    continue
                new_urls.add(url)
                seen_square_urls.add(url)

                for entry in show_matches:
                    row = {"title": entry["title"], "key": entry["key"], "slug": entry["slug"],
                           "cover": url, "source": "schedule_calendar", "month": month}
                    cover_rows[(row["key"], url)] = row
                for entry in episode_matches:
                    row = {"title": entry["title"], "key": entry["key"], "slug": "",
                           "cover": url, "source": "schedule_calendar_episode", "month": month}
                    cover_rows[(row["key"], url)] = row

        rss.emit("schedule_calendar_month_scanned", month=month,
                 visible_images=len(snap.get("images", [])),
                 new_square_urls=len(new_urls), unique_square_urls=len(seen_square_urls),
                 catalog_entries=len(catalog), url=rss.safe_url(snap.get("url") or rss.SCHEDULE_URL))

        nxt = next_month_control(snap)
        if not nxt:
            rss.emit("schedule_calendar_navigation_stopped", reason="next_month_control_not_found",
                     month=month, months_scanned=len(scanned_months),
                     controls=[{"text": row.get("text"), "aria": row.get("aria"),
                                "title": row.get("title"), "class": row.get("cls"),
                                "href": row.get("href")} for row in snap.get("controls", [])
                               if re.search(r"next|month|calendar|›|»|→", str(row).lower())][:20])
            break

        old_month, old_url = month, snap.get("url", "")
        try:
            page.evaluate("""index => {
              const visible=e=>{const s=getComputedStyle(e);return s.display!=='none'&&s.visibility!=='hidden'&&e.getClientRects().length>0};
              const all=[...document.querySelectorAll('button,a,[role=button],input[type=button]')].filter(visible);
              if(!all[index]) return false;
              all[index].click(); return true;
            }""", nxt["index"])
            page.wait_for_timeout(800)
            advanced = None
            for _ in range(3):
                advanced = rss._schedule_calendar_snapshot(page)
                new_month = current_month(advanced)
                if (new_month and new_month != old_month) or advanced.get("url") != old_url:
                    break
                page.wait_for_timeout(600)
            new_month = current_month(advanced or {})
            if not advanced or ((new_month == old_month or not new_month) and advanced.get("url") == old_url):
                rss.emit("schedule_calendar_navigation_stopped", reason="month_did_not_change",
                         month=old_month, control=nxt)
                break
        except Exception as exc:
            rss.emit("schedule_calendar_navigation_error", month=old_month, error=type(exc).__name__)
            break

    result = list(cover_rows.values())
    rss.emit("schedule_calendar_cover_index_complete", months_scanned=len(scanned_months),
             months=scanned_months, unique_square_urls=len(seen_square_urls),
             cover_associations=len(result), catalog_entries=len(catalog),
             sample=[{"title": row["title"], "cover": rss.safe_url(row["cover"]),
                      "source": row["source"], "month": row["month"]} for row in result[:12]])
    return {f"{row['key']}|{row['cover']}": row for row in result}


def apply_calendar_covers(items, session, page):
    covers = discover_calendar_covers(session, items, page)
    if not covers:
        return {"matched": 0, "replaced": 0, "unmatched": len(items), "rejected": 0}
    live_cache = {}
    matched = replaced = rejected = 0
    for item in items:
        choice = rss._match_schedule_cover(item.get("title", ""), covers)
        if not choice:
            continue
        matched += 1
        url = choice["cover"]
        if not rss._schedule_image_is_live(session, url, live_cache):
            rejected += 1
            rss.emit("schedule_cover_kept_existing", title=item.get("title", ""),
                     show=choice.get("title", ""), reason="calendar_cover_not_live")
            continue
        previous = item.get("cover", "")
        item["cover"] = url
        if previous != url:
            replaced += 1
        rss.emit("schedule_calendar_cover_applied", title=item.get("title", ""),
                 show=choice.get("title", ""), changed=(previous != url), cover=rss.safe_url(url))
    summary = {"matched": matched, "replaced": replaced,
               "unmatched": len(items) - matched, "rejected": rejected}
    rss.emit("schedule_calendar_cover_update_complete", episodes=len(items), **summary)
    return summary


def replace_cover_tags(xml_text: str, items, blocks):
    """Replace only per-item image href values; unmatched item XML stays verbatim."""
    output = []
    cursor = 0
    changed = 0
    for index, match in enumerate(blocks):
        output.append(xml_text[cursor:match.start()])
        block = match.group(0)
        new_cover = items[index].get("cover") or ""
        old_match = IMAGE_RE.search(block)
        old_cover = html.unescape(old_match.group(1)) if old_match else ""
        if new_cover and new_cover != old_cover:
            tag = f'<itunes:image href="{rss.xml_escape(new_cover)}"/>'
            if old_match:
                block = IMAGE_RE.sub(lambda _m: tag, block, count=1)
            else:
                enclosure = ENCLOSURE_RE.search(block)
                if not enclosure:
                    raise RuntimeError(f"Cannot add cover tag: missing enclosure for {items[index]['title']}.")
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
        page = browser.new_page(user_agent=rss.core.HEADERS["User-Agent"])
        try:
            # Public schedule only. No login attempts and no audio URL re-resolution.
            summary = apply_calendar_covers(items, session, page)
        finally:
            browser.close()

    updated, changed = replace_cover_tags(original, items, blocks)
    if changed == 0:
        rss.emit("test_rss_calendar_cover_refresh_no_changes", items=len(items),
                 matched=summary.get("matched", 0), replaced=summary.get("replaced", 0),
                 unmatched=summary.get("unmatched", len(items)),
                 note="Existing test-rss.xml was left untouched.")
        return

    updated = re.sub(
        r"<lastBuildDate>[\\s\\S]*?</lastBuildDate>",
        f"<lastBuildDate>{format_datetime(datetime.now(timezone.utc), usegmt=True)}</lastBuildDate>",
        updated, count=1,
    )
    new_root = ET.fromstring(updated)
    new_items = new_root.findall("./channel/item")
    if len(new_items) != rss.TARGET_ITEMS:
        raise RuntimeError("Updated test RSS no longer contains exactly 50 items.")
    for old_item, new_item in zip(entries, new_items):
        if (old_item.findtext("title") or "") != (new_item.findtext("title") or ""):
            raise RuntimeError("Cover refresh unexpectedly changed titles or item order.")
        old_enclosure, new_enclosure = old_item.find("enclosure"), new_item.find("enclosure")
        if old_enclosure is None or new_enclosure is None or old_enclosure.attrib != new_enclosure.attrib:
            raise RuntimeError("Cover refresh unexpectedly changed an MP3 enclosure.")
        if (old_item.findtext("description") or "") != (new_item.findtext("description") or ""):
            raise RuntimeError("Cover refresh unexpectedly changed an episode description.")

    FEED_PATH.write_text(updated, encoding="utf-8")
    rss.emit("test_rss_calendar_cover_refresh_written",
             path=str(FEED_PATH.relative_to(ROOT)), items=len(new_items),
             covers_changed=changed, matched=summary.get("matched", 0),
             unmatched=summary.get("unmatched", len(items)), production_rss_touched=False,
             audio_urls_changed=False, descriptions_changed=False)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        rss.emit("fatal", error=f"{type(exc).__name__}: {exc}")
        sys.exit(1)
