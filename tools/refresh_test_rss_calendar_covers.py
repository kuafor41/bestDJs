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
from email.utils import format_datetime, parsedate_to_datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse
import xml.etree.ElementTree as ET

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import build_test_rss as rss

FEED_PATH = ROOT / "public" / "test-rss.xml"
ITEM_RE = re.compile(r"<item>[\s\S]*?</item>")
IMAGE_RE = re.compile(r'<itunes:image\b[^>]*?href="([^"]*)"[^>]*/>')
TITLE_RE = re.compile(r"<title>([\s\S]*?)</title>")
LINK_RE = re.compile(r"<link>([\s\S]*?)</link>")
ENCLOSURE_RE = re.compile(r"<enclosure\b")
ENCLOSURE_URL_RE = re.compile(r'<enclosure\b[^>]*?\burl="([^"]*)"')
AUDIO_ARTWORK_DIR = ROOT / "public" / "episode-artwork"
AUDIO_ARTWORK_URL = "https://raw.githubusercontent.com/kuafor41/bestDJs/main/public/episode-artwork"
AUDIO_ARTWORK_MAX_BYTES = 8 * 1024 * 1024
MONTH_RE = re.compile(r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\s*,?\s*(20\d{2})\b", re.I)


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
        enclosure_url_match = ENCLOSURE_URL_RE.search(block)
        if not title_match:
            raise RuntimeError("An existing test-RSS item has no title.")
        items.append({
            "title": decode_element_text(title_match.group(1)),
            "page": decode_element_text(link_match.group(1)) if link_match else "",
            "cover": html.unescape(cover_match.group(1)) if cover_match else "",
            "audio": html.unescape(enclosure_url_match.group(1)) if enclosure_url_match else "",
        })
    return items, blocks


def schedule_snapshot(page):
    """Capture rendered headings, controls, image tags, CSS backgrounds, and iframe metadata."""
    return page.evaluate("""() => {
      const visible = e => {
        if (!e) return false;
        const s = window.getComputedStyle(e);
        return s.display !== 'none' && s.visibility !== 'hidden' &&
               Number(s.opacity || 1) > 0 && e.getClientRects().length > 0;
      };
      const clean = (v, n = 260) => String(v || '')
        .split(String.fromCharCode(13)).join(' ')
        .split(String.fromCharCode(10)).join(' ')
        .split(String.fromCharCode(9)).join(' ')
        .trim().slice(0, n);
      const cls = e => typeof e.className === 'string' ? e.className : (e.getAttribute('class') || '');
      const contextsFor = el => {
        const contexts = [];
        let p = el.parentElement;
        for (let depth = 0; p && depth < 5; depth++, p = p.parentElement) {
          const ls = [...p.querySelectorAll('a[href]')].slice(0, 6).map(a => ({
            text: clean(a.innerText || a.getAttribute('aria-label') || a.title, 100),
            title: clean(a.title || a.getAttribute('aria-label'), 100),
            href: a.href || ''
          }));
          contexts.push({
            text: clean(p.innerText || p.textContent, 260),
            cls: clean(cls(p), 120),
            links: ls
          });
        }
        return contexts;
      };
      const headings = [...document.querySelectorAll("h1,h2,h3,h4,[aria-live],[class*='month' i],[class*='calendar-title' i]")]
        .filter(visible).map(e => clean(e.innerText || e.textContent, 100)).filter(Boolean);
      const controls = [...document.querySelectorAll("button,a,[role='button'],input[type='button']")]
        .filter(visible).map((e, index) => ({
          index, tag: e.tagName.toLowerCase(),
          text: clean(e.innerText || e.value || e.textContent, 50),
          aria: clean(e.getAttribute('aria-label'), 100),
          title: clean(e.getAttribute('title'), 100),
          cls: clean(cls(e), 160), href: e.href || '',
          disabled: Boolean(e.disabled || e.getAttribute('aria-disabled') === 'true'),
          parentClass: clean(cls(e.parentElement), 120)
        }));
      const links = [...document.querySelectorAll('a[href]')].filter(visible).map(a => ({
        text: clean(a.innerText || a.getAttribute('aria-label') || a.title, 100),
        title: clean(a.title || a.getAttribute('aria-label'), 100),
        href: a.href || ''
      }));
      const images = [...document.images].filter(img => visible(img) &&
        (img.currentSrc || img.src || img.getAttribute('data-src') || img.getAttribute('data-lazy-src')))
        .map(img => {
          const sources = [];
          for (const attr of ['currentSrc','src','data-src','data-lazy-src','data-original']) {
            const v = attr === 'currentSrc' ? img.currentSrc : img.getAttribute(attr);
            if (v) sources.push(v);
          }
          for (const attr of ['srcset','data-srcset']) {
            const value = img.getAttribute(attr) || '';
            for (const part of value.split(',')) {
              const candidate = part.trim().split(String.fromCharCode(32))[0];
              if (candidate) sources.push(candidate);
            }
          }
          return {
            sources: [...new Set(sources)],
            alt: clean(img.alt, 120), title: clean(img.title, 120),
            width: img.naturalWidth || img.width || 0,
            height: img.naturalHeight || img.height || 0,
            contexts: contextsFor(img)
          };
        });
      const backgrounds = [...document.querySelectorAll('*')].filter(visible).map(el => {
        const bg = window.getComputedStyle(el).backgroundImage || '';
        if (!bg || bg === 'none' || !bg.includes('url(')) return null;
        const sources = [];
        for (const piece of bg.split('url(').slice(1)) {
          const value = piece.split(')')[0].replaceAll('"', '').replaceAll("'", '').trim();
          if (value) sources.push(value);
        }
        if (!sources.length) return null;
        return {
          sources: [...new Set(sources)],
          alt: clean(el.getAttribute('aria-label') || el.getAttribute('alt'), 120),
          title: clean(el.getAttribute('title'), 120),
          width: el.clientWidth || 0,
          height: el.clientHeight || 0,
          cls: clean(cls(el), 120),
          contexts: contextsFor(el)
        };
      }).filter(Boolean);
      const iframes = [...document.querySelectorAll('iframe')].filter(visible).map(el => ({
        src: el.src || '', title: clean(el.title, 120),
        cls: clean(cls(el), 120), width: el.clientWidth || 0, height: el.clientHeight || 0
      }));
      return {
        url: location.href,
        title: clean(document.title, 120),
        body: clean(document.body ? document.body.innerText : '', 10000),
        headings, controls, links, images, backgrounds, iframes
      };
    }""");

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
        if re.search(r"\b(previous|prev|back|left)\b", label):
            continue
        score = 0
        if re.search(r"\b(next month|next|forward|following month)\b", label):
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


def previous_month_control(snapshot):
    """Prefer an explicitly labelled previous-month control."""
    ranked = []
    for row in snapshot.get("controls", []):
        if row.get("disabled"):
            continue
        label = " ".join(str(row.get(k, "")) for k in (
            "aria", "title", "cls", "text", "href", "parentClass"
        )).lower()
        if re.search(r"\b(next|forward|following)\b", label):
            continue
        score = 0
        if re.search(r"\b(previous month|previous|prev|back|last month)\b", label):
            score += 100
        if re.search(r"fc-prev|prev-month|calendar-prev|month-prev|evo-prev|evcal.*prev", label):
            score += 120
        if row.get("text", "").strip() in ("<", "‹", "«", "❮", "←", "⟵"):
            score += 12
        if re.search(r"calendar|month|schedule|event|fc-|evo|evcal", label):
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


def archive_episode_title_keys():
    """Return normalized episode titles from the production archive as a strict scope list."""
    production_path = ROOT / "public" / "rss.xml"
    if not production_path.exists():
        raise RuntimeError("Cannot scope calendar covers: public/rss.xml is missing.")
    root = ET.parse(production_path).getroot()
    entries = root.findall("./channel/item")
    keys = {
        rss._compact_cover_label(entry.findtext("title") or "")
        for entry in entries
        if rss._compact_cover_label(entry.findtext("title") or "")
    }
    if not keys:
        raise RuntimeError("Cannot scope calendar covers: production RSS has no episode titles.")
    rss.emit("schedule_calendar_archive_scope",
             archive_items=len(entries), distinct_episode_titles=len(keys),
             production_rss_touched=False)
    return keys


def archive_history_month_count():
    """Derive the historical scan window from the untouched production RSS archive."""
    production_path = ROOT / "public" / "rss.xml"
    if not production_path.exists():
        raise RuntimeError("Cannot determine calendar history range: public/rss.xml is missing.")
    root = ET.parse(production_path).getroot()
    entries = root.findall("./channel/item")
    dates = []
    for entry in entries:
        raw = (entry.findtext("pubDate") or "").strip()
        if not raw:
            continue
        try:
            value = parsedate_to_datetime(raw)
        except (TypeError, ValueError, OverflowError):
            continue
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        dates.append(value.astimezone(timezone.utc))
    if not dates:
        raise RuntimeError("Cannot determine calendar history range: production RSS has no parseable pubDates.")
    oldest = min(dates)
    now = datetime.now(timezone.utc)
    count = max(1, (now.year - oldest.year) * 12 + now.month - oldest.month + 1)
    rss.emit("schedule_calendar_archive_range",
             archive_items=len(entries), oldest_episode_date=oldest.date().isoformat(),
             current_utc_month=now.strftime("%B, %Y"), history_months_requested=count,
             production_rss_touched=False)
    return min(count, 240)


def _discover_calendar_covers_direction(session, items, page, direction, max_months):
    """Collect square images from one direction of the rendered schedule calendar."""
    if direction not in ("forward", "backward"):
        raise ValueError(f"Unsupported calendar direction: {direction}")
    try:
        page.goto(rss.SCHEDULE_URL, wait_until="domcontentloaded", timeout=45000)
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        page.wait_for_timeout(800)
    except Exception as exc:
        rss.emit("schedule_calendar_unavailable", direction=direction, error=type(exc).__name__)
        return {}

    archive_title_keys = archive_episode_title_keys()
    catalog = {}
    cover_rows = {}
    dimension_cache = {}
    seen_signatures = set()
    seen_square_urls = set()
    scanned_months = []
    directly_matched_items = set()
    item_keys = [
        {"key": rss._compact_cover_label(item.get("title", "")), "title": item.get("title", "")}
        for item in items
        if rss._compact_cover_label(item.get("title", "")) in archive_title_keys
    ]

    def add_link(href, title, base_url):
        slug = rss._schedule_slug(href, base_url)
        title = (title or "").strip()
        key = rss._compact_cover_label(title)
        # Keep only calendar mixshows whose names prefix at least one episode
        # in the production archive; unrelated site/calendar shows are ignored.
        belongs_to_archive = bool(key) and any(episode_key.startswith(key) for episode_key in archive_title_keys)
        if slug and key and len(key) >= 5 and belongs_to_archive:
            catalog[(slug, key)] = {"slug": slug, "title": title, "key": key, "url": href}

    def capture_snapshot():
        snap = schedule_snapshot(page)
        # The calendar content may be embedded separately from the page header/footer.
        for frame in page.frames[1:]:
            try:
                frame_snap = schedule_snapshot(frame)
                if frame_snap.get("images") or frame_snap.get("backgrounds") or frame_snap.get("headings"):
                    for record in frame_snap.get("images", []) + frame_snap.get("backgrounds", []):
                        record["base_url"] = frame_snap.get("url") or frame.url
                    snap.setdefault("images", []).extend(frame_snap.get("images", []))
                    snap.setdefault("backgrounds", []).extend(frame_snap.get("backgrounds", []))
                    snap.setdefault("links", []).extend(frame_snap.get("links", []))
            except Exception as frame_exc:
                rss.emit("schedule_calendar_frame_error", direction=direction,
                         frame=rss.safe_url(frame.url), error=type(frame_exc).__name__)
        return snap

    def collect_month(snap, month_index):
        month = current_month(snap) or f"Month view {month_index + 1}"
        for link in snap.get("links", []):
            add_link(link.get("href", ""), link.get("text") or link.get("title", ""),
                     snap.get("url") or rss.SCHEDULE_URL)

        image_records = snap.get("images", []) + snap.get("backgrounds", [])
        signature = (
            month,
            tuple(sorted(src for img in image_records for src in img.get("sources", []) if src)),
        )
        if signature in seen_signatures:
            rss.emit("schedule_calendar_navigation_stopped", direction=direction,
                     reason="repeated_month_content", month=month)
            return False
        seen_signatures.add(signature)
        scanned_months.append(month)

        new_urls = set()
        for img in image_records:
            labels = [img.get("alt", ""), img.get("title", "")]
            for context in img.get("contexts", []):
                context_text = context.get("text", "")
                if context_text and len(context_text) <= 260 and len(context.get("links", [])) <= 6:
                    labels.append(context_text)
                for link in context.get("links", []):
                    labels.extend([link.get("text", ""), link.get("title", "")])
                    if link.get("href"):
                        labels.append(urlparse(link["href"]).path.rsplit("/", 1)[-1])
            normalized = [rss._compact_cover_label(value) for value in labels
                          if value and len(rss._compact_cover_label(value)) >= 5]

            for raw in img.get("sources", []):
                url = urljoin(img.get("base_url") or snap.get("url") or rss.SCHEDULE_URL,
                              html.unescape(str(raw or "").strip()))
                parsed = urlparse(url)
                if parsed.scheme not in ("http", "https") or not re.search(
                    r"\.(?:jpe?g|png|webp|avif|gif)$", parsed.path, re.I
                ):
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
                score = rss._square_variant_score(
                    session, url, img.get("width", 0), img.get("height", 0), dimension_cache
                )
                if score is None:
                    continue

                new_urls.add(url)
                seen_square_urls.add(url)
                def store_cover_row(row):
                    # Backward traversal is newest-to-oldest. The first square
                    # cover found for a show is therefore the archive's preferred
                    # latest-known artwork; never let older variants replace it.
                    if direction == "backward" and any(
                        existing.get("key") == row["key"] for existing in cover_rows.values()
                    ):
                        return
                    cover_rows[(row["key"], row["cover"])] = row

                for entry in show_matches:
                    row = {"title": entry["title"], "key": entry["key"], "slug": entry["slug"],
                           "cover": url, "source": "schedule_calendar", "month": month}
                    store_cover_row(row)
                for entry in episode_matches:
                    directly_matched_items.add(entry["title"])
                    row = {"title": entry["title"], "key": entry["key"], "slug": "",
                           "cover": url, "source": "schedule_calendar_episode", "month": month}
                    store_cover_row(row)

        rss.emit("schedule_calendar_month_scanned",
                 direction=direction, month=month, visible_images=len(snap.get("images", [])),
                 visible_backgrounds=len(snap.get("backgrounds", [])),
                 new_square_urls=len(new_urls), unique_square_urls=len(seen_square_urls),
                 catalog_entries=len(catalog), cover_associations=len(cover_rows),
                 direct_episode_matches=sorted(directly_matched_items)[:40],
                 url=rss.safe_url(snap.get("url") or rss.SCHEDULE_URL))
        return True

    for month_index in range(max_months):
        try:
            snap = capture_snapshot()
        except Exception as exc:
            rss.emit("schedule_calendar_snapshot_error", direction=direction,
                     month_index=month_index, error=type(exc).__name__)
            break

        month = current_month(snap) or f"Month view {month_index + 1}"
        if not collect_month(snap, month_index):
            break

        if month_index >= max_months - 1:
            rss.emit("schedule_calendar_direction_complete", direction=direction,
                     months_scanned=len(scanned_months), requested_months=max_months,
                     first_month=scanned_months[0] if scanned_months else "",
                     last_month=scanned_months[-1] if scanned_months else "",
                     unique_square_urls=len(seen_square_urls),
                     cover_associations=len(cover_rows), catalog_entries=len(catalog))
            break

        control = next_month_control(snap) if direction == "forward" else previous_month_control(snap)
        if not control:
            rss.emit("schedule_calendar_navigation_stopped", direction=direction,
                     reason="month_navigation_control_not_found", month=month,
                     months_scanned=len(scanned_months), requested_months=max_months,
                     controls=[{"text": row.get("text"), "aria": row.get("aria"),
                                "title": row.get("title"), "class": row.get("cls"),
                                "href": row.get("href")} for row in snap.get("controls", [])
                               if re.search(r"next|prev|previous|month|calendar|›|»|→|‹|«|←",
                                            str(row).lower())][:20])
            break

        old_month, old_url = month, snap.get("url", "")
        try:
            page.evaluate("""index => {
              const visible=e=>{const s=getComputedStyle(e);return s.display!=='none'&&s.visibility!=='hidden'&&e.getClientRects().length>0};
              const all=[...document.querySelectorAll('button,a,[role=button],input[type=button]')].filter(visible);
              if(!all[index]) return false;
              all[index].click(); return true;
            }""", control["index"])
            page.wait_for_timeout(800)
            advanced = None
            for _ in range(3):
                advanced = schedule_snapshot(page)
                new_month = current_month(advanced)
                if (new_month and new_month != old_month) or advanced.get("url") != old_url:
                    break
                page.wait_for_timeout(600)
            new_month = current_month(advanced or {})
            if not advanced or ((new_month == old_month or not new_month) and advanced.get("url") == old_url):
                rss.emit("schedule_calendar_navigation_stopped", direction=direction,
                         reason="month_did_not_change", month=old_month, control=control,
                         months_scanned=len(scanned_months), requested_months=max_months)
                break
        except Exception as exc:
            rss.emit("schedule_calendar_navigation_error", direction=direction,
                     month=old_month, error=type(exc).__name__)
            break

    result = list(cover_rows.values())
    rss.emit("schedule_calendar_cover_direction_index_complete", direction=direction,
             months_scanned=len(scanned_months), months=scanned_months,
             unique_square_urls=len(seen_square_urls), cover_associations=len(result),
             catalog_entries=len(catalog), directly_matched_items=sorted(directly_matched_items))
    return {f"{row['key']}|{row['cover']}": row for row in result}


def discover_calendar_covers(session, items, page):
    """Index forward schedule images and the archive's entire historical month range."""
    history_months = archive_history_month_count()
    forward = _discover_calendar_covers_direction(
        session, items, page, direction="forward", max_months=18
    )
    backward = _discover_calendar_covers_direction(
        session, items, page, direction="backward", max_months=history_months
    )
    # For each archive mixshow, use the first cover encountered during the
    # newest-to-oldest scan. Only if no historical association exists do we use
    # the first forward-scan association as a fallback.
    selected_by_key = {}
    for row in backward.values():
        selected_by_key.setdefault(row["key"], row)
    for row in forward.values():
        selected_by_key.setdefault(row["key"], row)
    combined = {f"{row['key']}|{row['cover']}": row for row in selected_by_key.values()}
    rows = list(combined.values())
    rss.emit("schedule_calendar_cover_index_complete",
             months_scanned_forward=18, months_requested_backward=history_months,
             cover_associations=len(rows),
             unique_square_urls=len({row["cover"] for row in rows}),
             distinct_title_keys=len({row["key"] for row in rows}),
             catalog_entries=len({(row.get("slug", ""), row.get("key", "")) for row in rows}),
             first_cover_per_archived_mixshow=True,
             scope="mixshows represented in public/rss.xml only",
             note="Newest-to-oldest historical first cover takes priority; production RSS read-only.")
    return combined


# Repair only the four pages whose artwork is refreshed from the source page by
# podcast clients but is not reliably represented by the schedule calendar.
SOURCE_PAGE_ARTWORK_KEYS = {
    "tritonia544",
    "musicboxradio939",
    "visionradios06e41",
    "altituderadio203",
}


def _production_archive_cover_index():
    """Return known cover URLs for exact archived episodes; never infer from a different show."""
    production_path = ROOT / "public" / "rss.xml"
    if not production_path.exists():
        return {"by_page": {}, "by_title": {}}
    try:
        root = ET.parse(production_path).getroot()
    except (ET.ParseError, OSError) as exc:
        rss.emit("episode_artwork_archive_read_error", error=type(exc).__name__)
        return {"by_page": {}, "by_title": {}}

    by_page, by_title = {}, {}
    for entry in root.findall("./channel/item"):
        title = (entry.findtext("title") or "").strip()
        link = (entry.findtext("link") or "").strip()
        cover = ""
        for child in entry:
            if child.tag.endswith("}image") and child.attrib.get("href"):
                cover = child.attrib["href"].strip()
                break
        if not cover:
            continue
        title_key = rss._compact_cover_label(title)
        page_key = urlparse(link).path.rstrip("/").lower()
        row = {"title": title, "page": link, "cover": cover}
        if page_key:
            by_page[page_key] = row
        if title_key and title_key not in by_title:
            by_title[title_key] = row
        elif title_key and by_title[title_key].get("cover") != cover:
            # Duplicate title with conflicting artwork is ambiguous; do not use it.
            by_title.pop(title_key, None)
    return {"by_page": by_page, "by_title": by_title}


def _supported_image_url(raw, base_url):
    url = urljoin(base_url, html.unescape(str(raw or "").strip()))
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return ""
    if not re.search(r"\.(?:jpe?g|png|webp|avif|gif)$", parsed.path, re.I):
        return ""
    return url


def _syncsafe_integer(data):
    if len(data) != 4 or any(value & 0x80 for value in data):
        return None
    return (data[0] << 21) | (data[1] << 14) | (data[2] << 7) | data[3]


def _image_extension(image_bytes, mime):
    mime = (mime or "").split(";", 1)[0].strip().lower()
    if image_bytes.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if image_bytes.startswith(b"RIFF") and image_bytes[8:12] == b"WEBP":
        return "webp"
    if image_bytes.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if mime in ("image/jpeg", "image/jpg"):
        return "jpg"
    if mime == "image/png":
        return "png"
    if mime == "image/webp":
        return "webp"
    if mime == "image/gif":
        return "gif"
    return ""


def _apic_frame_image(frame_data):
    """Return (picture_type, mime, bytes) from an ID3v2 APIC frame."""
    if len(frame_data) < 5:
        return None
    encoding = frame_data[0]
    mime_end = frame_data.find(b"\x00", 1)
    if mime_end < 0 or mime_end + 1 >= len(frame_data):
        return None
    mime = frame_data[1:mime_end].decode("latin-1", errors="replace")
    picture_type = frame_data[mime_end + 1]
    description_start = mime_end + 2
    if encoding in (1, 2):
        image_start = -1
        for pos in range(description_start, len(frame_data) - 1, 2):
            if frame_data[pos:pos + 2] == b"\x00\x00":
                image_start = pos + 2
                break
    else:
        desc_end = frame_data.find(b"\x00", description_start)
        image_start = desc_end + 1 if desc_end >= 0 else -1
    if image_start < description_start or image_start >= len(frame_data):
        return None
    image_bytes = frame_data[image_start:]
    if not _image_extension(image_bytes, mime):
        return None
    return picture_type, mime, image_bytes


def extract_id3_front_cover(data):
    """Parse front-cover images from an ID3v2 tag at the start of an MP3."""
    if len(data) < 10 or data[:3] != b"ID3":
        return None, "no_id3v2_header"
    version = data[3]
    if version not in (2, 3, 4):
        return None, f"unsupported_id3v2_version_{version}"
    tag_size = _syncsafe_integer(data[6:10])
    if tag_size is None:
        return None, "invalid_id3_tag_size"
    tag_end = 10 + tag_size
    if tag_end > len(data):
        return None, "id3_tag_larger_than_downloaded_range"
    pos = 10
    if data[5] & 0x40:
        if version == 3:
            if pos + 4 > tag_end:
                return None, "truncated_extended_header"
            pos += 4 + int.from_bytes(data[pos:pos + 4], "big")
        elif version == 4:
            if pos + 4 > tag_end:
                return None, "truncated_extended_header"
            extended_size = _syncsafe_integer(data[pos:pos + 4])
            if extended_size is None:
                return None, "invalid_extended_header"
            pos += extended_size
        else:
            pos += 6

    images = []
    while pos < tag_end:
        if version == 2:
            if pos + 6 > tag_end:
                break
            frame_id = data[pos:pos + 3]
            if frame_id == b"\x00\x00\x00":
                break
            frame_size = int.from_bytes(data[pos + 3:pos + 6], "big")
            header_size = 6
        else:
            if pos + 10 > tag_end:
                break
            frame_id = data[pos:pos + 4]
            if frame_id == b"\x00\x00\x00\x00":
                break
            frame_size = (_syncsafe_integer(data[pos + 4:pos + 8]) if version == 4
                          else int.from_bytes(data[pos + 4:pos + 8], "big"))
            if frame_size is None:
                return None, "invalid_id3_frame_size"
            header_size = 10
        if frame_size <= 0 or pos + header_size + frame_size > tag_end:
            break
        frame = data[pos + header_size:pos + header_size + frame_size]
        if (version == 2 and frame_id == b"PIC") or (version in (3, 4) and frame_id == b"APIC"):
            if version == 2 and len(frame) >= 6:
                fmt = frame[1:4].decode("latin-1", errors="replace").lower()
                mime = {"jpg": "image/jpeg", "png": "image/png", "gif": "image/gif"}.get(fmt, f"image/{fmt}")
                parsed = _apic_frame_image(b"\x03" + mime.encode("latin-1") + b"\x00" + frame[4:])
            else:
                parsed = _apic_frame_image(frame)
            if parsed:
                images.append(parsed)
        pos += header_size + frame_size
    if not images:
        return None, "no_embedded_picture_frame"
    images.sort(key=lambda value: (value[0] != 3, value[0]))
    picture_type, mime, image_bytes = images[0]
    return {"picture_type": picture_type, "mime": mime, "bytes": image_bytes}, "embedded_picture_found"


def _extract_audio_embedded_cover(session, item):
    """Extract a small ID3 cover from an already-resolved TopDJMixes MP3 enclosure."""
    audio_url = (item.get("audio") or "").strip()
    title = (item.get("title") or "").strip()
    if not audio_url:
        rss.emit("episode_audio_artwork_probe_skipped", title=title, reason="no_enclosure_url")
        return None
    parsed_url = urlparse(audio_url)
    if parsed_url.scheme != "https" or parsed_url.hostname not in ("files.topdjmixes.com", "www.topdjmixes.com"):
        rss.emit("episode_audio_artwork_probe_skipped", title=title, reason="unexpected_audio_host")
        return None

    response = None
    try:
        response = session.get(
            audio_url,
            headers={**rss.core.HEADERS, "Referer": item.get("page") or rss.SCHEDULE_URL,
                     "Range": f"bytes=0-{AUDIO_ARTWORK_MAX_BYTES - 1}"},
            stream=True, allow_redirects=True, timeout=(15, 35),
        )
        status = int(getattr(response, "status_code", 0) or 0)
        content_type = str(response.headers.get("Content-Type", "")).split(";", 1)[0].strip().lower()
        if status not in (200, 206) or content_type not in (
            "audio/mpeg", "audio/mp3", "application/octet-stream"
        ):
            rss.emit("episode_audio_artwork_probe_failed", title=title, status=status,
                     content_type=content_type, reason="audio_range_unavailable")
            return None
        data = response.raw.read(AUDIO_ARTWORK_MAX_BYTES, decode_content=True)
    except (requests.RequestException, OSError, ValueError, TypeError) as exc:
        rss.emit("episode_audio_artwork_probe_failed", title=title, error=type(exc).__name__)
        return None
    finally:
        if response is not None:
            try:
                response.close()
            except Exception:
                pass

    parsed_cover, reason = extract_id3_front_cover(data)
    if not parsed_cover:
        rss.emit("episode_audio_artwork_probe_complete", title=title, result=reason,
                 bytes_read=len(data), artwork_found=False)
        return None

    image_bytes = parsed_cover["bytes"]
    extension = _image_extension(image_bytes, parsed_cover["mime"])
    if not extension or not image_bytes or len(image_bytes) > 8 * 1024 * 1024:
        rss.emit("episode_audio_artwork_probe_complete", title=title, result="invalid_or_oversized_cover",
                 bytes_read=len(data), artwork_found=False)
        return None
    import hashlib
    image_hash = hashlib.sha256(image_bytes).hexdigest()
    title_key = rss._compact_cover_label(title) or "episode"
    filename = f"{title_key}-{image_hash[:12]}.{extension}"
    destination = AUDIO_ARTWORK_DIR / filename
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists() or destination.read_bytes() != image_bytes:
        destination.write_bytes(image_bytes)
    cover_url = f"{AUDIO_ARTWORK_URL}/{filename}"
    rss.emit("episode_audio_artwork_probe_complete", title=title, result=reason,
             picture_type=parsed_cover["picture_type"], mime=parsed_cover["mime"],
             image_bytes=len(image_bytes), sha256=image_hash, artwork_found=True,
             cover=rss.safe_url(cover_url), local_path=str(destination.relative_to(ROOT)))
    return {"url": cover_url, "source": "mp3_id3_front_cover", "sha256": image_hash}


def _source_page_artwork_candidate(session, item):
    """Find artwork explicitly tied to the episode/show on its own page.

    Prefer the primary linked image (or the episode-specific video thumbnail) over
    a small schedule-card variant such as '*-600x600.webp'. No unrelated image is
    accepted merely because it appears somewhere on the page.
    """
    page_url = (item.get("page") or "").strip()
    title = (item.get("title") or "").strip()
    if not page_url or not title:
        return None

    response = None
    try:
        response = session.get(
            page_url,
            headers={**rss.core.HEADERS, "Referer": rss.SCHEDULE_URL},
            allow_redirects=True,
            timeout=(15, 35),
        )
        if response.status_code < 200 or response.status_code >= 400:
            rss.emit("episode_artwork_page_unavailable", title=title,
                     status=response.status_code, page=rss.safe_url(page_url))
            return None
        soup = BeautifulSoup(response.text, "html.parser")
        base_url = response.url or page_url
    except (requests.RequestException, OSError, ValueError) as exc:
        rss.emit("episode_artwork_page_error", title=title,
                 page=rss.safe_url(page_url), error=type(exc).__name__)
        return None
    finally:
        if response is not None:
            try:
                response.close()
            except Exception:
                pass

    episode_key = rss._compact_cover_label(title)
    show_name = re.sub(r"\s+\d{1,5}[a-z]?$", "", title, flags=re.I).strip()
    show_key = rss._compact_cover_label(show_name) or episode_key
    candidates = {}

    def add(raw, score, source, evidence):
        url = _supported_image_url(raw, base_url)
        if not url:
            return
        score = int(score)
        existing = candidates.get(url)
        row = {"url": url, "score": score, "source": source, "evidence": evidence}
        if existing is None or score > existing["score"]:
            candidates[url] = row

    def compact(value):
        return rss._compact_cover_label(value)

    # OpenGraph/Twitter image is useful only when its URL is show-specific, or
    # the page's own title confirms that this is the exact requested episode.
    meta_title = " ".join([
        soup.title.get_text(" ", strip=True) if soup.title else "",
        soup.select_one('meta[property="og:title"]')["content"]
        if soup.select_one('meta[property="og:title"]') and soup.select_one('meta[property="og:title"]').get("content")
        else "",
    ])
    meta_title_key = compact(meta_title)
    for selector in (
        'meta[property="og:image"]',
        'meta[property="og:image:secure_url"]',
        'meta[name="twitter:image"]',
        'meta[name="twitter:image:src"]',
    ):
        node = soup.select_one(selector)
        if not node or not node.get("content"):
            continue
        url = _supported_image_url(node.get("content"), base_url)
        url_key = compact(urlparse(url).path.rsplit("/", 1)[-1]) if url else ""
        if url and show_key and show_key in url_key:
            add(url, 920, "episode_page_metadata", "show name in image URL")
        elif url and episode_key and episode_key in meta_title_key:
            add(url, 860, "episode_page_metadata", "exact episode in page metadata title")

    # For TopDJMixes pages the prominent artwork is often an image wrapped in a
    # link to its full-size original. Prefer that linked image over the smaller
    # <img src> variant. It is the same primary page media a client can discover
    # when it refreshes the linked episode page.
    for node in soup.find_all("img"):
        alt_title = " ".join([
            node.get("alt", ""),
            node.get("title", ""),
            node.get("aria-label", ""),
        ])
        anchor = node.find_parent("a", href=True)
        anchor_text = ""
        anchor_href = ""
        if anchor:
            anchor_text = " ".join([
                anchor.get("title", ""),
                anchor.get("aria-label", ""),
                anchor.get_text(" ", strip=True),
            ])
            anchor_href = urljoin(base_url, html.unescape(anchor.get("href", "").strip()))
        labels_key = compact(" ".join([
            alt_title,
            anchor_text,
            anchor_href,
            urlparse(anchor_href).path.rsplit("/", 1)[-1] if anchor_href else "",
        ]))
        exact_episode_evidence = bool(episode_key and episode_key in labels_key)
        show_evidence = bool(show_key and show_key in labels_key)

        if anchor_href and show_evidence:
            linked_image = _supported_image_url(anchor_href, base_url)
            if linked_image:
                add(linked_image, 1450, "episode_page_primary_linked_image",
                    "image link title/filename matches mixshow")
        elif anchor_href and exact_episode_evidence:
            linked_image = _supported_image_url(anchor_href, base_url)
            if linked_image:
                add(linked_image, 1350, "episode_page_episode_linked_image",
                    "image link identifies exact episode")

        raw_sources = []
        for attr in ("data-src", "data-lazy-src", "data-original", "src"):
            if node.get(attr):
                raw_sources.append(node.get(attr))
        for attr in ("srcset", "data-srcset"):
            raw = node.get(attr, "")
            if raw:
                for entry in raw.split(","):
                    parts = entry.strip().split()
                    if parts:
                        raw_sources.append(parts[0])
        for raw in raw_sources:
            image_url = _supported_image_url(raw, base_url)
            if not image_url:
                continue
            image_key = compact(urlparse(image_url).path.rsplit("/", 1)[-1])
            if exact_episode_evidence:
                add(image_url, 1300, "episode_specific_page_image",
                    "image alt/title/link identifies exact episode")
            elif show_key and show_key in image_key:
                score = 790
                if re.search(r"[-_]600x600\.", urlparse(image_url).path, re.I):
                    score -= 350
                elif not re.search(r"[-_]\d{2,5}x\d{2,5}\.", urlparse(image_url).path, re.I):
                    score += 80
                add(image_url, score, "episode_page_show_image",
                    "show name in image URL")
            elif show_evidence:
                add(image_url, 700, "episode_page_labelled_image",
                    "show name in image alt/title/link")

    if not candidates:
        rss.emit("episode_artwork_page_no_candidate", title=title,
                 page=rss.safe_url(base_url), show_key=show_key)
        return None

    for candidate in sorted(candidates.values(), key=lambda row: row["score"], reverse=True):
        if rss.core.cover_url_is_live(session, candidate["url"], referer=base_url):
            rss.emit("episode_artwork_page_candidate_selected", title=title,
                     source=candidate["source"], evidence=candidate["evidence"],
                     score=candidate["score"], cover=rss.safe_url(candidate["url"]),
                     candidate_count=len(candidates))
            return candidate
        rss.emit("episode_artwork_page_candidate_rejected", title=title,
                 source=candidate["source"], evidence=candidate["evidence"],
                 cover=rss.safe_url(candidate["url"]), reason="image URL not live")
    return None


def repair_known_episode_page_covers(items, session):
    """Prefer the MP3's embedded front cover, then page artwork, then exact archive fallback."""
    archive = _production_archive_cover_index()
    matched = replaced = kept = embedded_found = 0
    for item in items:
        title = (item.get("title") or "").strip()
        title_key = rss._compact_cover_label(title)
        if title_key not in SOURCE_PAGE_ARTWORK_KEYS:
            continue
        matched += 1
        old_cover = item.get("cover") or ""
        embedded = _extract_audio_embedded_cover(session, item)
        candidate = None
        source = ""
        new_cover = ""
        if embedded:
            new_cover = embedded["url"]
            source = embedded["source"]
            embedded_found += 1
        else:
            candidate = _source_page_artwork_candidate(session, item)
            new_cover = candidate["url"] if candidate else ""
            source = candidate["source"] if candidate else ""

        if not new_cover:
            page_key = urlparse(item.get("page") or "").path.rstrip("/").lower()
            archived = archive["by_page"].get(page_key) or archive["by_title"].get(title_key)
            if archived and archived.get("cover") and archived["cover"] != old_cover:
                if rss.core.cover_url_is_live(session, archived["cover"], referer=item.get("page") or rss.SCHEDULE_URL):
                    new_cover = archived["cover"]
                    source = "exact_same_episode_production_archive"
                    rss.emit("episode_artwork_archive_fallback_selected", title=title,
                             archived_title=archived.get("title", ""), cover=rss.safe_url(new_cover))
        if not new_cover:
            kept += 1
            rss.emit("episode_artwork_repair_kept_existing", title=title,
                     reason="no_embedded_page_artwork_or_exact_archive_cover",
                     existing_cover=rss.safe_url(old_cover) if old_cover else None)
            continue
        if new_cover == old_cover:
            kept += 1
            rss.emit("episode_artwork_repair_kept_existing", title=title,
                     reason="source_selected_same_url", source=source, cover=rss.safe_url(old_cover))
            continue

        previous = old_cover
        item["cover"] = new_cover
        replaced += 1
        rss.emit("episode_artwork_repair_applied", title=title, source=source,
                 previous_cover=rss.safe_url(previous) if previous else None,
                 cover=rss.safe_url(new_cover))
    result = {"targeted": matched, "replaced": replaced, "kept_existing": kept,
              "embedded_covers_found": embedded_found}
    rss.emit("episode_artwork_repair_complete", **result,
             scope=sorted(SOURCE_PAGE_ARTWORK_KEYS), production_rss_touched=False)
    return result


def apply_calendar_covers(items, session, page):
    covers = discover_calendar_covers(session, items, page)
    if not covers:
        return {"matched": 0, "replaced": 0, "unmatched": len(items), "rejected": 0}
    live_cache = {}
    matched = replaced = rejected = 0
    for item in items:
        choice = rss._match_schedule_cover(item.get("title", ""), covers)
        if not choice:
            title = item.get("title", "")
            episode_key = rss._compact_cover_label(title)
            prefix_rows = [row for row in covers.values()
                           if row.get("key") and episode_key.startswith(row["key"])]
            if not prefix_rows:
                reason = "no_matching_calendar_association"
                candidate_rows = []
            else:
                longest = max(len(row["key"]) for row in prefix_rows)
                candidate_rows = [row for row in prefix_rows if len(row["key"]) == longest]
                reason = ("conflicting_cover_variants"
                          if len({row.get("cover") for row in candidate_rows}) > 1
                          else "matcher_rule_rejected_candidate")
            rss.emit("schedule_calendar_cover_unmatched", title=title, reason=reason,
                     candidate_variants=[{"show": row.get("title", ""),
                                          "month": row.get("month", ""),
                                          "source": row.get("source", ""),
                                          "cover": rss.safe_url(row.get("cover", ""))}
                                         for row in candidate_rows[:8]])
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
    unmatched_titles = [item.get("title", "") for item in items
                        if not rss._match_schedule_cover(item.get("title", ""), covers)]
    summary = {"matched": matched, "replaced": replaced,
               "unmatched": len(items) - matched, "rejected": rejected}
    rss.emit("schedule_calendar_cover_update_complete", episodes=len(items),
             unmatched_titles=unmatched_titles, **summary)
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
                block = block[:enclosure.start()] + tag + "\n" + block[enclosure.start():]
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

    # These four items can expose their preferred media through the linked episode
    # page rather than the schedule card; repair only those explicit cases.
    page_summary = repair_known_episode_page_covers(items, session)
    summary["episode_page_repair"] = page_summary

    updated, changed = replace_cover_tags(original, items, blocks)
    if changed == 0:
        rss.emit("test_rss_calendar_cover_refresh_no_changes", items=len(items),
                 matched=summary.get("matched", 0), replaced=summary.get("replaced", 0),
                 unmatched=summary.get("unmatched", len(items)),
                 note="Existing test-rss.xml was left untouched.")
        return

    updated = re.sub(
        r"<lastBuildDate>[\s\S]*?</lastBuildDate>",
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
