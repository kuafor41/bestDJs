#!/usr/bin/env python3
"""Build an isolated 50-item TopDJMixes test RSS via its authenticated Download flow."""
from __future__ import annotations

import html
import json
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import urlparse, urljoin

import requests
from bs4 import BeautifulSoup
import build_feed as core

BASE = core.BASE
REST = BASE + "/wp-json/wp/v2/posts"
OUT = Path("public/test-rss.xml")
TARGET_ITEMS = 50
REST_PAGES_LIMIT = 5
CANDIDATES_TARGET = 100
PER_PAGE = 100
PAUSE = float(__import__("os").environ.get("TEST_FEED_REQUEST_DELAY", "0.2"))
USER = core.USER
PASSWORD = core.PASSWORD


def emit(event: str, **fields):
    print(json.dumps({"event": event, **fields}, ensure_ascii=False, sort_keys=True), flush=True)


def safe_url(url: str):
    p = urlparse(url)
    return {"host": p.netloc, "path": p.path, "query_present": bool(p.query),
            "url_if_no_query": url if not p.query else None}


def parse_datetime_value(value: str | None):
    if not value:
        return None
    value = value.strip()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        pass
    for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y",
                "%Y-%m-%d", "%d-%m-%Y %H:%M", "%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def page_date(text: str, soup: BeautifulSoup, rest_date: str):
    # Prefer the site's own Added label, then its visible Date label.
    patterns = (
        (r"\bAdded\s*:\s*([A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4}|\d{4}-\d{2}-\d{2}|\d{1,2}[-/.]\d{1,2}[-/.]\d{4})", "Added"),
        (r"\bDate\s*:\s*([A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4}|\d{4}-\d{2}-\d{2}|\d{1,2}[-/.]\d{1,2}[-/.]\d{4})", "Date"),
    )
    for pattern, source in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            parsed = parse_datetime_value(match.group(1))
            if parsed:
                return parsed, source
    for node in soup.select("time[datetime], meta[property='article:published_time'], meta[name='date'], meta[itemprop='datePublished']"):
        value = node.get("datetime") or node.get("content")
        parsed = parse_datetime_value(value)
        if parsed:
            return parsed, "page_metadata"
    parsed = parse_datetime_value(rest_date)
    if parsed:
        return parsed, "wordpress_date_fallback"
    return None, "missing"


def extract_title(soup: BeautifulSoup, fallback: str):
    for selector in ("h1.entry-title", "article h1", ".entry-title", "h1"):
        node = soup.select_one(selector)
        value = core.clean_text(node.get_text(" ", strip=True)) if node else ""
        if value:
            return re.sub(r"\s*[|–-]\s*Top\s*DJ\s*Mixes.*$", "", value, flags=re.I).strip() or fallback
    node = soup.select_one('meta[property="og:title"]')
    if node and node.get("content"):
        return core.clean_text(node["content"])
    return fallback


def extract_description(soup: BeautifulSoup, title: str):
    meta_values = []
    for selector in ('meta[property="og:description"]', 'meta[name="description"]',
                     'meta[name="twitter:description"]'):
        node = soup.select_one(selector)
        if node and node.get("content"):
            value = core.clean_text(node["content"])
            if value and value.lower() not in (title.lower(), "top dj mixes"):
                meta_values.append(value)

    entry = soup.select_one(".entry-content") or soup.select_one("article")
    chunks = []
    if entry:
        for child in entry.children:
            if getattr(child, "get", None) and child.get("id") == "download-link":
                break
            if getattr(child, "get", None) and child.get("id") in ("advert", "advert-mobile"):
                continue
            text = core.clean_text(child.get_text(" ", strip=True) if hasattr(child, "get_text") else str(child))
            if not text:
                continue
            if text.upper() == "DOWNLOAD" or text.lower().startswith("report broken link"):
                continue
            chunks.append(text)
    body_description = core.clean_text(" | ".join(chunks))
    # Prefer the actual tracklist and episode details, while keeping a useful SEO summary if
    # the page body is unusually sparse.
    if len(body_description) >= 80:
        if meta_values and meta_values[0].lower() not in body_description.lower():
            return (meta_values[0] + "\n\n" + body_description)[:10000]
        return body_description[:10000]
    if meta_values:
        return meta_values[0][:4000]
    return body_description[:4000] or title


def extract_cover(soup: BeautifulSoup, page_url: str):
    candidates = []
    def add(raw, source, width=0, height=0):
        raw = core.clean_text(raw)
        if not raw or raw.startswith("data:"):
            return
        url = urljoin(page_url, html.unescape(raw))
        p = urlparse(url)
        if p.scheme not in ("http", "https"):
            return
        if not re.search(r"\.(?:jpe?g|png|webp)(?:$|\?)", p.path, re.I):
            return
        name = (p.path + " " + p.netloc).lower()
        score = 70 if source == "meta" else 80
        if source == "content":
            score += 22
        if any(t in name for t in ("cover", "artwork", "episode", "mix", "upload")):
            score += 12
        if width and height and width >= 250 and height >= 250:
            score += 12
        if any(t in name for t in ("logo", "avatar", "favicon", "banner", "advert", "ads-", "spinner", "placeholder")):
            score -= 120
        candidates.append((score, url, source))

    for selector in ('meta[property="og:image"]', 'meta[property="og:image:secure_url"]',
                     'meta[name="twitter:image"]', 'meta[name="twitter:image:src"]'):
        node = soup.select_one(selector)
        if node and node.get("content"):
            add(node["content"], "meta")
    for node in soup.select(".entry-content img, article img, .post-thumbnail img, .wp-post-image"):
        raw = (node.get("data-src") or node.get("data-lazy-src") or node.get("data-original")
               or node.get("src") or "")
        width = int(node.get("width", "0")) if str(node.get("width", "0")).isdigit() else 0
        height = int(node.get("height", "0")) if str(node.get("height", "0")).isdigit() else 0
        add(raw, "content", width, height)
        srcset = node.get("srcset", "")
        if srcset:
            last = srcset.split(",")[-1].strip().split(" ")[0]
            add(last, "content", width, height)
    # Don't invent episode artwork or return a generic site logo as the cover.
    candidates = sorted(set(candidates), key=lambda x: x[0], reverse=True)
    return candidates[0][1] if candidates and candidates[0][0] > 0 else None


def is_episode_page(soup: BeautifulSoup):
    control = soup.select_one("a.wpdm-download-link.download-on-click")
    if not control:
        return False
    # Reject accidental links to profile pages and posts that merely mention downloading.
    body = core.clean_text((soup.select_one(".entry-content") or soup).get_text(" ", strip=True)).lower()
    looks_like_mix = (
        "audio:" in body and "size:" in body
    ) or "parts" in body
    return looks_like_mix


def wordpress_posts(session: requests.Session):
    all_posts = []
    seen = set()
    for page_num in range(1, REST_PAGES_LIMIT + 1):
        url = REST
        params = {
            "per_page": PER_PAGE, "page": page_num, "orderby": "date", "order": "desc",
            "_fields": "link,date,date_gmt,slug,title,excerpt",
        }
        try:
            response = session.get(url, params=params, headers={"Referer": BASE + "/mix-shows/"}, timeout=30)
        except requests.RequestException as exc:
            emit("rest_page_error", page=page_num, error=type(exc).__name__)
            break
        if response.status_code == 400 and page_num > 1:
            break
        response.raise_for_status()
        try:
            posts = response.json()
        except ValueError:
            emit("rest_page_not_json", page=page_num, status=response.status_code,
                 content_type=response.headers.get("Content-Type", ""))
            break
        if not isinstance(posts, list) or not posts:
            break
        for post in posts:
            link = post.get("link", "")
            parsed = urlparse(link)
            if parsed.netloc.lower() not in ("www.topdjmixes.com", "topdjmixes.com"):
                continue
            canonical = link.split("#", 1)[0].rstrip("/") + "/"
            if canonical not in seen:
                seen.add(canonical)
                all_posts.append({**post, "link": canonical})
        total_pages = int(response.headers.get("X-WP-TotalPages", str(page_num)))
        emit("rest_posts_page", page=page_num, returned=len(posts),
             total_posts_seen=len(all_posts), advertised_total_pages=total_pages)
        if page_num >= total_pages or len(all_posts) >= 500:
            break
        time.sleep(PAUSE)
    return all_posts


def inspect_candidates(session: requests.Session, posts):
    candidates = []
    checked_pages = 0
    for post in posts:
        if len(candidates) >= CANDIDATES_TARGET:
            break
        slug = str(post.get("slug") or urlparse(post["link"]).path.strip("/").split("/")[-1])
        # Episode posts almost always carry a number in the slug. Keep this as a speed filter,
        # then let the actual TopDJMixes Download control determine whether the page is an episode.
        if not re.search(r"\d", slug):
            continue
        try:
            response = session.get(post["link"], headers={"Referer": BASE + "/mix-shows/"}, timeout=30)
            checked_pages += 1
            if response.status_code >= 400:
                emit("episode_page_http_error", page=safe_url(post["link"]), status=response.status_code)
                continue
        except requests.RequestException as exc:
            emit("episode_page_error", page=safe_url(post["link"]), error=type(exc).__name__)
            continue
        soup = BeautifulSoup(response.text, "html.parser")
        if not is_episode_page(soup):
            time.sleep(PAUSE)
            continue
        title = extract_title(soup, core.clean_text((post.get("title") or {}).get("rendered", "")) or slug)
        visible_text = core.clean_text((soup.select_one(".entry-content") or soup).get_text(" ", strip=True))
        date, date_source = page_date(visible_text, soup, post.get("date_gmt") or post.get("date"))
        if not date:
            emit("episode_skipped_missing_date", page=safe_url(post["link"]), title=title)
            time.sleep(PAUSE)
            continue
        description = extract_description(soup, title)
        cover = extract_cover(soup, response.url)
        item = {
            "title": title, "page": response.url, "slug": slug, "date": date,
            "date_source": date_source, "description": description, "cover": cover,
            "wordpress_date": post.get("date_gmt") or post.get("date"),
        }
        candidates.append(item)
        emit("episode_candidate", page=safe_url(item["page"]), title=title,
             date=date.isoformat(), date_source=date_source,
             description_chars=len(description), cover_found=bool(cover),
             cover=safe_url(cover) if cover else None,
             tracklist_terms=("parts" in visible_text.lower() or "tracklist" in visible_text.lower()))
        time.sleep(PAUSE)
    emit("episode_candidates_complete", candidates=len(candidates), pages_checked=checked_pages)
    return candidates


def resolve_audio(session: requests.Session, item):
    # This is the exact endpoint pattern that was observed from clicking the site's own
    # a.wpdm-download-link.download-on-click control. Use Range=0-0 so only one byte can be read.
    endpoint = urljoin(BASE, "/download/" + item["slug"].strip("/") + "/")
    response = None
    try:
        response = session.get(
            endpoint,
            headers={**core.HEADERS, "Referer": item["page"], "Range": "bytes=0-0"},
            allow_redirects=True, stream=True, timeout=(15, 35),
        )
        final_url = response.url
        parsed = urlparse(final_url)
        content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        content_range = response.headers.get("Content-Range", "")
        total = None
        match = re.search(r"/(\d+|\*)$", content_range)
        if match and match.group(1) != "*":
            total = int(match.group(1))
        if response.status_code == 206:
            first_byte = next(response.iter_content(1), b"")
            got_byte = len(first_byte) == 1
        else:
            got_byte = response.status_code == 200
        valid = (
            got_byte and response.status_code in (200, 206)
            and (content_type.startswith("audio/") or core.is_mp3(final_url))
            and core.is_mp3(final_url)
            and not parsed.path.lower().endswith((".html", ".htm"))
        )
        if not total and response.status_code == 200:
            length = response.headers.get("Content-Length", "")
            if length.isdigit():
                total = int(length)
        emit("episode_audio_range_check", title=item["title"],
             page=safe_url(item["page"]), endpoint_path=urlparse(endpoint).path,
             final_host=parsed.netloc, final_path=parsed.path, final_query_present=bool(parsed.query),
             status=response.status_code, content_type=content_type,
             content_range=content_range, total_bytes=total,
             range_requested="bytes=0-0", redirect_count=len(response.history), valid=valid)
        if valid:
            return final_url, total
        return None, None
    except requests.RequestException as exc:
        emit("episode_audio_error", title=item["title"], page=safe_url(item["page"]),
             endpoint_path=urlparse(endpoint).path, error=type(exc).__name__)
        return None, None
    finally:
        if response is not None:
            response.close()


def xml_escape(value):
    return html.escape(str(value or ""), quote=True)


def write_feed(items):
    OUT.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">',
        '<channel>',
        '<title>bestDJs Podcast — TEST (50 Episodes)</title>',
        '<link>https://www.topdjmixes.com/mix-shows/</link>',
        '<description>TEST FEED for review: 50 recent TopDJMixes episodes. Do not use as the production feed.</description>',
        '<language>en-us</language>',
        '<itunes:author>bestDJs</itunes:author>',
        '<itunes:category text="Music"/>',
        '<itunes:explicit>no</itunes:explicit>',
        f'<lastBuildDate>{format_datetime(now, usegmt=True)}</lastBuildDate>',
    ]
    if items and items[0].get("cover"):
        lines.append(f'<itunes:image href="{xml_escape(items[0]["cover"])}"/>')
    for item in items:
        lines.extend([
            '<item>',
            f'<title>{xml_escape(item["title"])}</title>',
            f'<link>{xml_escape(item["page"])}</link>',
            f'<guid isPermaLink="true">{xml_escape(item["page"])}</guid>',
            f'<pubDate>{format_datetime(item["date"], usegmt=True)}</pubDate>',
            f'<description>{xml_escape(item["description"])}</description>',
        ])
        if item.get("cover"):
            lines.append(f'<itunes:image href="{xml_escape(item["cover"])}"/>')
        lines.extend([
            f'<enclosure url="{xml_escape(item["audio"])}" length="{int(item.get("audio_length") or 0)}" type="audio/mpeg"/>',
            '<itunes:explicit>no</itunes:explicit>',
            '</item>',
        ])
    lines.extend(['</channel>', '</rss>', ''])
    OUT.write_text("\n".join(lines), encoding="utf-8")


def validate_feed(items):
    root = ET.parse(OUT).getroot()
    entries = root.findall("./channel/item")
    if len(entries) != TARGET_ITEMS:
        raise RuntimeError(f"Test feed has {len(entries)} items, expected exactly {TARGET_ITEMS}.")
    dates = []
    cover_count = 0
    for entry in entries:
        title = entry.findtext("title") or ""
        date = entry.findtext("pubDate") or ""
        enclosure = entry.find("enclosure")
        if not title or not entry.findtext("description") or not date:
            raise RuntimeError("RSS item is missing its title, description or publication date.")
        if enclosure is None or enclosure.get("type") != "audio/mpeg":
            raise RuntimeError(f"RSS item missing MP3 enclosure: {title}")
        if not core.is_mp3(enclosure.get("url", "")):
            raise RuntimeError(f"RSS enclosure is not a direct MP3 URL: {title}")
        parsed = datetime.strptime(date, "%a, %d %b %Y %H:%M:%S GMT").replace(tzinfo=timezone.utc)
        dates.append(parsed)
        if entry.find("{http://www.itunes.com/dtds/podcast-1.0.dtd}image") is not None:
            cover_count += 1
    if dates != sorted(dates, reverse=True):
        raise RuntimeError("RSS items are not sorted newest to oldest.")
    emit("test_rss_validated", path=str(OUT), items=len(entries), cover_items=cover_count,
         oldest=dates[-1].isoformat(), newest=dates[0].isoformat(),
         sorted_newest_to_oldest=True)


def main():
    if not USER or not PASSWORD:
        raise RuntimeError("SOURCE_USERNAME and SOURCE_PASSWORD secrets are required.")
    session = requests.Session()
    session.headers.update(core.HEADERS)
    core.configure_retries(session)

    # Reuse the proven normal site login; authenticate() clicks the site Download button once
    # and confirms that the Download endpoint resolves to an MP3 without reading its body.
    listing, _browser_catalog = core.authenticate(session)
    emit("authenticated_session_ready", listing_status=listing.status_code,
         listing_path=urlparse(listing.url).path)

    posts = wordpress_posts(session)
    emit("wordpress_post_candidates", total=len(posts))
    candidates = inspect_candidates(session, posts)
    if len(candidates) < TARGET_ITEMS:
        raise RuntimeError(f"Only {len(candidates)} TopDJMixes Download-enabled episode pages were found; need {TARGET_ITEMS}.")

    candidates.sort(key=lambda item: item["date"], reverse=True)
    result = []
    failures = 0
    for item in candidates:
        audio, length = resolve_audio(session, item)
        if audio:
            item["audio"] = audio
            item["audio_length"] = length or 0
            result.append(item)
        else:
            failures += 1
        if len(result) >= TARGET_ITEMS:
            break
        time.sleep(PAUSE)
    if len(result) < TARGET_ITEMS:
        raise RuntimeError(f"Only {len(result)} episodes yielded direct MP3s; need {TARGET_ITEMS}.")
    result.sort(key=lambda item: item["date"], reverse=True)
    write_feed(result)
    validate_feed(result)
    emit("test_feed_written", file=str(OUT), episodes=len(result),
         missing_covers=sum(1 for item in result if not item.get("cover")),
         missing_description=sum(1 for item in result if not item.get("description")),
         audio_failures=failures,
         newest_title=result[0]["title"], newest_date=result[0]["date"].isoformat(),
         newest_audio_host=urlparse(result[0]["audio"]).netloc)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        emit("fatal", error=f"{type(exc).__name__}: {exc}")
        sys.exit(1)
