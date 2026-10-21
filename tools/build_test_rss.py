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
from urllib.parse import urlparse, urljoin, parse_qs
from http.cookies import SimpleCookie

import requests
from bs4 import BeautifulSoup
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import build_feed as core

BASE = core.BASE
REST = BASE + "/wp-json/wp/v2/posts"
SCHEDULE_URL = BASE + "/schedule/"
OUT = Path("public/test-rss.xml")
TARGET_ITEMS = 50
REST_PAGES_LIMIT = 4
CANDIDATES_TARGET = 75
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
    """Copy the episode's Date-to-tracklist block, preserving source line breaks."""
    entry = soup.select_one(".entry-content") or soup.select_one("article")
    if not entry:
        return title

    def visible_text(node):
        clone = BeautifulSoup(str(node), "html.parser")
        for br in clone.find_all("br"):
            br.replace_with("\n")
        for li in clone.find_all("li"):
            li.insert_before("\n")
            li.append("\n")
        text = clone.get_text("", strip=False)
        lines = []
        for line in text.replace("\r", "").split("\n"):
            line = re.sub(r"[\t ]+", " ", line).strip()
            if line:
                lines.append(line)
        return "\n".join(lines)

    nodes = list(entry.children)
    start_index = None
    for i, node in enumerate(nodes):
        if not getattr(node, "get_text", None):
            continue
        text = visible_text(node)
        if re.match(r"^Date\s*:", text, re.I):
            start_index = i
            break

    if start_index is None:
        for selector in ("p", "div", "section"):
            for node in entry.select(selector):
                text = visible_text(node)
                if re.match(r"^Date\s*:", text, re.I):
                    return text[:10000]
        return title

    chunks = []
    for node in nodes[start_index:]:
        if getattr(node, "get", None) and node.get("id") in ("download-link", "advert", "advert-mobile"):
            break
        if getattr(node, "get", None) and node.name in ("script", "style", "nav", "footer"):
            continue
        text = visible_text(node) if getattr(node, "get_text", None) else str(node).strip()
        if not text or text.upper() == "DOWNLOAD" or text.lower().startswith("report broken link"):
            continue
        if chunks and text == chunks[-1]:
            continue
        chunks.append(text)

    description = "\n".join(chunks).strip()

    # In the isolated 50-episode test feed, leave exactly two blank lines
    # between the Size line and the tracklist (or its explicit heading).
    # Do not alter descriptions that have no tracklist.
    size_line = re.search(r"(?im)^Size\s*:[^\n]*", description)
    if size_line:
        tail = description[size_line.end():]
        if re.search(r"(?im)^\s*(?:Tracklist\s*:|\d{1,3}[.)]\s+)", tail):
            description = (
                description[:size_line.end()].rstrip()
                + "\n\n\n"
                + tail.lstrip("\r\n")
            )

    return description[:10000] if description else title


def extract_cover(soup: BeautifulSoup, page_url: str, episode_title: str):
    """Prefer artwork whose filename/alt/title matches this show's name, not generic page art."""
    candidates_by_url = {}

    def normalize(value):
        value = re.sub(r"\.(?:jpe?g|png|webp)(?:$|\?).*$", "", value, flags=re.I)
        value = re.sub(r"\b(?:episode|ep|show|radio|podcast)\s*#?\d+\b", " ", value, flags=re.I)
        return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()

    show_name = re.sub(r"\s+(?:episode\s*)?#?\d+[a-z]?$", "", episode_title, flags=re.I).strip()
    show_tokens = set(normalize(show_name).split())

    def add(raw, source, alt="", title="", width=0, height=0):
        raw = html.unescape(str(raw or "").strip())
        if not raw or raw.startswith("data:"):
            return
        url = urljoin(page_url, raw)
        p = urlparse(url)
        if p.scheme not in ("http", "https") or not re.search(r"\.(?:jpe?g|png|webp)(?:$|\?)", p.path, re.I):
            return
        path_label = normalize(p.path.rsplit("/", 1)[-1])
        label = normalize(" ".join((path_label, alt, title)))
        label_tokens = set(label.split())
        overlap = len(show_tokens & label_tokens)
        score = 0
        if show_tokens and overlap:
            score += 100 + 15 * overlap
            if show_tokens.issubset(label_tokens):
                score += 100
        if source == "content":
            score += 20
        if width >= 250 and height >= 250:
            score += 5
        if any(t in label for t in ("logo", "avatar", "favicon", "banner", "advert", "spinner", "placeholder")):
            score -= 200
        candidates_by_url[url] = max(score, candidates_by_url.get(url, -999))

    for selector in ('meta[property="og:image"]', 'meta[property="og:image:secure_url"]',
                     'meta[name="twitter:image"]', 'meta[name="twitter:image:src"]'):
        node = soup.select_one(selector)
        if node and node.get("content"):
            add(node["content"], "meta", node.get("alt", ""), node.get("title", ""))

    entry = soup.select_one(".entry-content") or soup.select_one("article")
    if entry:
        for node in entry.select("img"):
            raw = (node.get("data-src") or node.get("data-lazy-src") or node.get("data-original")
                   or node.get("src") or "")
            width = int(node.get("width", "0")) if str(node.get("width", "0")).isdigit() else 0
            height = int(node.get("height", "0")) if str(node.get("height", "0")).isdigit() else 0
            add(raw, "content", node.get("alt", ""), node.get("title", ""), width, height)
            srcset = node.get("srcset", "")
            if srcset:
                last = srcset.split(",")[-1].strip().split(" ")[0]
                add(last, "content", node.get("alt", ""), node.get("title", ""), width, height)

    ranked = sorted(candidates_by_url.items(), key=lambda pair: pair[1], reverse=True)
    if not ranked or ranked[0][1] < 100:
        return None
    return ranked[0][0]


def _compact_cover_label(value):
    """Normalize show names and image filenames while ignoring WordPress size suffixes."""
    value = html.unescape(str(value or "")).lower().strip()
    value = re.sub(r"[?#].*$", "", value)
    value = re.sub(r"\.(?:jpe?g|png|webp|avif|gif)$", "", value, flags=re.I)
    value = re.sub(r"[-_]\d{2,5}x\d{2,5}$", "", value)
    return re.sub(r"[^a-z0-9]+", "", value)


def _schedule_slug(href, base_url=None):
    url = urljoin(base_url or SCHEDULE_URL, str(href or "").strip())
    parsed = urlparse(url)
    if parsed.hostname not in ("www.topdjmixes.com", "topdjmixes.com"):
        return ""
    path = parsed.path.strip("/")
    if not path or "/" in path:
        return ""
    slug = path.lower()
    ignored = {
        "mix-shows", "music-player", "news", "linklists", "schedule",
        "mobile-app", "login", "register", "lost-password", "wp-login.php",
    }
    if slug in ignored or re.search(r"\.(?:jpe?g|png|webp|avif|gif|css|js)$", slug, re.I):
        return ""
    return slug


def _image_dimensions_from_header(data):
    """Read common raster dimensions from a short byte range without downloading the full image."""
    if not data:
        return None
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    if data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
        return int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little")
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP" and len(data) >= 30:
        kind = data[12:16]
        if kind == b"VP8X":
            return (
                1 + int.from_bytes(data[24:27], "little"),
                1 + int.from_bytes(data[27:30], "little"),
            )
        if kind == b"VP8L" and len(data) >= 25 and data[20] == 0x2f:
            b1, b2, b3, b4 = data[21:25]
            width = 1 + b1 + ((b2 & 0x3f) << 8)
            height = 1 + ((b2 & 0xc0) >> 6) + (b3 << 2) + ((b4 & 0x0f) << 10)
            return width, height
        if kind == b"VP8 " and len(data) >= 30 and data[23:26] == b"\x9d\x01\x2a":
            width = int.from_bytes(data[26:28], "little") & 0x3fff
            height = int.from_bytes(data[28:30], "little") & 0x3fff
            return width, height
    if data.startswith(b"\xff\xd8"):
        index = 2
        sof_markers = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
        while index + 9 < len(data):
            if data[index] != 0xFF:
                index += 1
                continue
            while index < len(data) and data[index] == 0xFF:
                index += 1
            if index >= len(data):
                break
            marker = data[index]
            index += 1
            if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7 or marker == 0x01:
                continue
            if index + 2 > len(data):
                break
            segment_length = int.from_bytes(data[index:index + 2], "big")
            if segment_length < 2:
                break
            if marker in sof_markers and index + 7 < len(data):
                height = int.from_bytes(data[index + 3:index + 5], "big")
                width = int.from_bytes(data[index + 5:index + 7], "big")
                return width, height
            index += segment_length
    return None


def _fetch_image_dimensions(session, url, cache):
    """Probe only the first 64 KiB and determine true pixel dimensions for a candidate cover."""
    if url in cache:
        return cache[url]
    dimensions = None
    response = None
    try:
        response = session.get(
            url,
            headers={**core.HEADERS, "Referer": SCHEDULE_URL, "Range": "bytes=0-65535"},
            allow_redirects=True,
            stream=True,
            timeout=(10, 20),
        )
        content_type = str(response.headers.get("Content-Type", "")).split(";", 1)[0].strip().lower()
        if response.status_code in (200, 206) and content_type.startswith("image/"):
            try:
                data = response.raw.read(65536, decode_content=True)
            except TypeError:
                data = response.raw.read(65536)
            dimensions = _image_dimensions_from_header(data)
    except (requests.RequestException, OSError, ValueError, TypeError) as exc:
        emit("cover_dimension_probe_error", cover=safe_url(url), error=type(exc).__name__)
    finally:
        if response is not None:
            try:
                response.close()
            except Exception:
                pass
    cache[url] = dimensions
    return dimensions


def _square_variant_score(session, url, width=0, height=0, dimension_cache=None):
    """Accept a cover only when its URL or decoded pixels establish a square image."""
    parsed = urlparse(url)
    if not re.search(r"\.(?:jpe?g|png|webp|avif|gif)$", parsed.path, re.I):
        return None
    match = re.search(r"[-_](\d{2,5})x(\d{2,5})\.[^.]+$", parsed.path, re.I)
    if match:
        w, h = int(match.group(1)), int(match.group(2))
        if w != h:
            return None
        return 100000 + min(w, 20000)
    cache = dimension_cache if dimension_cache is not None else {}
    dimensions = _fetch_image_dimensions(session, url, cache)
    if dimensions and dimensions[0] == dimensions[1] and dimensions[0] >= 64:
        return 50000 + min(dimensions[0], 20000)
    return None


def _image_source_urls(node):
    """Read rendered image URLs and responsive alternatives, not the enclosing anchor's wide href."""
    values = []
    for attr in ("data-src", "data-lazy-src", "data-original", "src", "data-background-image"):
        raw = node.get(attr)
        if raw:
            values.append(raw)
    for attr in ("srcset", "data-srcset"):
        raw = node.get(attr, "")
        if raw:
            for entry in raw.split(","):
                parts = entry.strip().split()
                if parts:
                    values.append(parts[0])
    style = node.get("style", "")
    if style:
        values.extend(
            match.strip().strip("'\"")
            for match in re.findall(r"url\((.*?)\)", style, re.I)
        )
    return values


def discover_schedule_covers(session, items, browser_page):
    """Browse the rendered schedule calendar month-by-month and index its square covers."""
    try:
        browser_page.goto(SCHEDULE_URL, wait_until="domcontentloaded", timeout=45000)
        try:
            browser_page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        browser_page.wait_for_timeout(800)
    except Exception as exc:
        emit("schedule_calendar_unavailable", error=type(exc).__name__)
        return {}

    month_re = re.compile(r"\\b(January|February|March|April|May|June|July|August|September|October|November|December)\\s*,?\\s*(20\\d{2})\\b", re.I)
    catalog = {}
    matches = {}
    dimension_cache = {}
    seen_signatures = set()
    seen_square_urls = set()
    scanned_months = []

    def snapshot():
        return browser_page.evaluate("""() => {
          const visible = e => {
            if (!e) return false;
            const s = getComputedStyle(e);
            return s.display !== 'none' && s.visibility !== 'hidden' && e.getClientRects().length > 0;
          };
          const clean = (v, n=260) => String(v || '').replace(/\\s+/g, ' ').trim().slice(0,n);
          const cls = e => typeof e.className === 'string' ? e.className : (e.getAttribute('class') || '');
          const headings = [...document.querySelectorAll('h1,h2,h3,h4,[aria-live],[class*=month i],[class*=calendar-title i]')]
            .filter(visible).map(e => clean(e.innerText || e.textContent,100)).filter(Boolean);
          const controls = [...document.querySelectorAll('button,a,[role=button],input[type=button]')]
            .filter(visible).map((e,index) => ({
              index, tag:e.tagName.toLowerCase(), text:clean(e.innerText || e.value || e.textContent,50),
              aria:clean(e.getAttribute('aria-label'),100), title:clean(e.getAttribute('title'),100),
              cls:clean(cls(e),160), href:e.href || '', disabled:Boolean(e.disabled || e.getAttribute('aria-disabled')==='true'),
              parentClass:clean(cls(e.parentElement),120)
            }));
          const links = [...document.querySelectorAll('a[href]')].filter(visible).map(a => ({
            text:clean(a.innerText || a.getAttribute('aria-label') || a.title,100),
            title:clean(a.title || a.getAttribute('aria-label'),100), href:a.href || ''
          }));
          const images = [...document.images].filter(visible).map(img => {
            const contexts=[];
            let p=img.parentElement;
            for(let i=0;p && i<5;i++,p=p.parentElement) {
              const t=clean(p.innerText || p.textContent,260);
              const ls=[...p.querySelectorAll('a[href]')].slice(0,6).map(a=>({
                text:clean(a.innerText || a.getAttribute('aria-label') || a.title,100),
                title:clean(a.title || a.getAttribute('aria-label'),100), href:a.href || ''
              }));
              contexts.push({text:t,cls:clean(cls(p),120),links:ls});
            }
            const sources=[];
            for(const attr of ['currentSrc','src','data-src','data-lazy-src','data-original']) {
              const v=attr==='currentSrc' ? img.currentSrc : img.getAttribute(attr);
              if(v) sources.push(v);
            }
            for(const attr of ['srcset','data-srcset']) {
              for(const part of (img.getAttribute(attr)||'').split(',')) {
                const v=part.trim().split(/\\s+/)[0]; if(v) sources.push(v);
              }
            }
            return {sources:[...new Set(sources)],alt:clean(img.alt,120),title:clean(img.title,120),
              width:img.naturalWidth||img.width||0,height:img.naturalHeight||img.height||0,contexts};
          });
          return {url:location.href,title:clean(document.title,120),body:clean(document.body?.innerText,2500),
            headings,controls,links,images};
        }""")

    def current_month(snap):
        for value in snap.get("headings", []):
            hit = month_re.search(value)
            if hit:
                return f"{hit.group(1).title()}, {hit.group(2)}"
        hit = month_re.search(snap.get("body", ""))
        return f"{hit.group(1).title()}, {hit.group(2)}" if hit else ""

    def add_catalog_link(href, title, base_url):
        slug = _schedule_slug(href, base_url)
        title = (title or "").strip()
        key = _compact_cover_label(title)
        if slug and key and len(key) >= 5:
            catalog[(slug,key)] = {"slug":slug,"title":title,"key":key,"url":href}

    def next_control(snap):
        ranked=[]
        for row in snap.get("controls", []):
            if row.get("disabled"):
                continue
            label=" ".join(str(row.get(k,"")) for k in ("aria","title","cls","text","href","parentClass")).lower()
            if re.search(r"\\b(previous|prev|back|left)\\b",label):
                continue
            score=0
            if re.search(r"\\b(next month|next|forward|following month)\\b",label):
                score += 100
            if re.search(r"fc-next|next-month|calendar-next|month-next",label):
                score += 120
            if row.get("text","").strip() in (">","›","»","❯","→","⟶"):
                score += 12
            if re.search(r"calendar|month|schedule|event|fc-",label):
                score += 8
            if row.get("href") and re.search(r"month|calendar|schedule|tribe-bar-date|[?&]m=",row["href"],re.I):
                score += 10
            if score:
                ranked.append((score,row))
        ranked.sort(key=lambda p:p[0],reverse=True)
        if not ranked:
            return None
        if len(ranked)>1 and ranked[0][0]==ranked[1][0] and ranked[0][1]["index"]!=ranked[1][1]["index"]:
            return None
        return ranked[0][1]

    max_months=18
    for month_index in range(max_months):
        try:
            snap=snapshot()
        except Exception as exc:
            emit("schedule_calendar_snapshot_error",month_index=month_index,error=type(exc).__name__)
            break
        month=current_month(snap) or f"Month view {month_index+1}"
        for link in snap.get("links",[]):
            add_catalog_link(link.get("href",""),link.get("text") or link.get("title",""),snap.get("url") or SCHEDULE_URL)
        signature=(month,tuple(sorted(src for im in snap.get("images",[]) for src in im.get("sources",[]) if src)))
        if signature in seen_signatures:
            emit("schedule_calendar_navigation_stopped",reason="repeated_month_content",month=month)
            break
        seen_signatures.add(signature)
        scanned_months.append(month)
        new_urls=set()
        for im in snap.get("images",[]):
            contexts=im.get("contexts",[])
            labels=[im.get("alt",""),im.get("title","")]
            for ctx in contexts:
                if ctx.get("text") and len(ctx["text"])<=260 and len(ctx.get("links",[]))<=6:
                    labels.append(ctx["text"])
                for link in ctx.get("links",[]):
                    labels.extend([link.get("text",""),link.get("title","")])
                    if link.get("href"):
                        labels.append(urlparse(link["href"]).path.rsplit("/",1)[-1])
            norm_labels=[_compact_cover_label(v) for v in labels if v and len(_compact_cover_label(v))>=5]
            for raw in im.get("sources",[]):
                url=urljoin(snap.get("url") or SCHEDULE_URL,html.unescape(str(raw or "").strip()))
                parsed=urlparse(url)
                if parsed.scheme not in ("http","https") or not re.search(r"\\.(?:jpe?g|png|webp|avif|gif)$",parsed.path,re.I):
                    continue
                filename_key=_compact_cover_label(parsed.path.rsplit("/",1)[-1])
                labels_for_match=norm_labels+([filename_key] if filename_key else [])
                if not labels_for_match:
                    continue
                possible=[]
                for entry in catalog.values():
                    if entry["key"] and any(entry["key"] in label or (len(label)>=12 and label in entry["key"]) for label in labels_for_match):
                        possible.append(entry)
                direct=[]
                for item in items:
                    key=_compact_cover_label(item.get("title",""))
                    if len(key)>=9 and any(key in label for label in labels_for_match):
                        direct.append((key,item.get("title","")))
                if not possible and not direct:
                    continue
                score=_square_variant_score(session,url,im.get("width",0),im.get("height",0),dimension_cache)
                if score is None:
                    continue
                new_urls.add(url)
                seen_square_urls.add(url)
                for entry in possible:
                    row={"title":entry["title"],"key":entry["key"],"slug":entry["slug"],
                         "cover":url,"source":"schedule_calendar","month":month}
                    matches[(row["key"],url)]=row
                for key,title in direct:
                    row={"title":title,"key":key,"slug":"","cover":url,
                         "source":"schedule_calendar_episode","month":month}
                    matches[(key,url)]=row

        emit("schedule_calendar_month_scanned",month=month,
             visible_images=len(snap.get("images",[])),new_square_urls=len(new_urls),
             unique_square_urls=len(seen_square_urls),catalog_entries=len(catalog),
             url=safe_url(snap.get("url") or SCHEDULE_URL))
        if month_index+1>=max_months:
            break
        nxt=next_control(snap)
        if not nxt:
            emit("schedule_calendar_navigation_stopped",reason="next_month_control_not_found",
                 month=month,months_scanned=len(scanned_months),
                 controls=[{"text":r.get("text"),"aria":r.get("aria"),"title":r.get("title"),
                            "class":r.get("cls"),"href":r.get("href")} for r in snap.get("controls",[]) if
                           re.search(r"next|month|calendar|›|»|→",str(r).lower())][:20])
            break
        old_month,old_url=month,snap.get("url","")
        try:
            browser_page.evaluate("""index => {
              const visible=e=>{const s=getComputedStyle(e);return s.display!=='none'&&s.visibility!=='hidden'&&e.getClientRects().length>0};
              const all=[...document.querySelectorAll('button,a,[role=button],input[type=button]')].filter(visible);
              if(!all[index]) return false;
              all[index].click(); return true;
            }""",nxt["index"])
            browser_page.wait_for_timeout(800)
            advanced=None
            for _ in range(3):
                advanced=snapshot()
                new_month=current_month(advanced)
                if (new_month and new_month!=old_month) or advanced.get("url")!=old_url:
                    break
                browser_page.wait_for_timeout(600)
            new_month=current_month(advanced or {})
            if not advanced or ((new_month==old_month or not new_month) and advanced.get("url")==old_url):
                emit("schedule_calendar_navigation_stopped",reason="month_did_not_change",month=old_month,control=nxt)
                break
        except Exception as exc:
            emit("schedule_calendar_navigation_error",month=old_month,error=type(exc).__name__)
            break

    result=list(matches.values())
    emit("schedule_calendar_cover_index_complete",months_scanned=len(scanned_months),
         months=scanned_months,unique_square_urls=len(seen_square_urls),
         cover_associations=len(result),catalog_entries=len(catalog),
         sample=[{"title":r["title"],"cover":safe_url(r["cover"]),"source":r["source"],"month":r["month"]} for r in result[:12]])
    return {f"{row['key']}|{row['cover']}":row for row in result}



def _match_schedule_cover(episode_title, schedule_covers):
    """Use the most specific schedule-calendar title; reject conflicting cover URLs."""
    episode_key = _compact_cover_label(episode_title)
    matches = [row for row in schedule_covers.values()
               if row.get("key") and episode_key.startswith(row["key"])]
    if not matches:
        return None
    longest = max(len(row["key"]) for row in matches)
    matches = [row for row in matches if len(row["key"]) == longest]
    if len({row["cover"] for row in matches}) != 1:
        return None
    matches.sort(key=lambda row: row.get("source") == "schedule_calendar_episode", reverse=True)
    return matches[0]



def _schedule_image_is_live(session, url, cache):
    """Do not replace a current cover with a dead URL or an HTML challenge page."""
    if url in cache:
        return cache[url]
    response = None
    valid = False
    try:
        response = session.get(
            url,
            headers={**core.HEADERS, "Referer": SCHEDULE_URL, "Range": "bytes=0-0"},
            allow_redirects=True,
            stream=True,
            timeout=(10, 20),
        )
        status = int(getattr(response, "status_code", 0) or 0)
        content_type = str(getattr(response, "headers", {}).get("Content-Type", "") or "")
        content_type = content_type.split(";", 1)[0].strip().lower()
        valid = status in (200, 206) and content_type.startswith("image/")
    except requests.RequestException as exc:
        emit("schedule_cover_probe_error", cover=safe_url(url), error=type(exc).__name__)
    finally:
        if response is not None:
            try:
                response.close()
            except Exception:
                pass
    cache[url] = valid
    return valid


def apply_schedule_covers(items, session):
    """Apply only matched, reachable square thumbnail URLs; keep every other old cover."""
    schedule_covers = discover_schedule_covers(session, items)
    if not schedule_covers:
        emit("schedule_cover_update_skipped", reason="no_square_schedule_covers")
        return {"matched": 0, "replaced": 0, "unmatched": len(items), "rejected": 0}

    probe_cache = {}
    matched = replaced = rejected = 0
    for item in items:
        choice = _match_schedule_cover(item.get("title", ""), schedule_covers)
        if not choice:
            continue
        matched += 1
        cover = choice["cover"]
        if not _schedule_image_is_live(session, cover, probe_cache):
            rejected += 1
            emit("schedule_cover_kept_existing", title=item.get("title", ""),
                 show=choice["title"], reason="square_cover_url_not_live")
            continue
        previous = str(item.get("cover") or "")
        item["cover"] = cover
        item["cover_source"] = "schedule_square"
        if previous != cover:
            replaced += 1
        emit("schedule_square_cover_applied", title=item.get("title", ""),
             show=choice["title"], changed=(previous != cover),
             previous_cover=safe_url(previous) if previous else None,
             cover=safe_url(cover))
    summary = {
        "matched": matched,
        "replaced": replaced,
        "unmatched": max(0, len(items) - matched),
        "rejected": rejected,
    }
    emit("schedule_cover_update_complete", episodes=len(items), **summary)
    return summary


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
        total_pages = int(response.headers.get("X-WP-TotalPages", str(REST_PAGES_LIMIT)))
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
        cover = extract_cover(soup, response.url, title)
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


def transfer_cookies(context, session):
    for cookie in context.cookies():
        if "topdjmixes.com" in cookie.get("domain", ""):
            session.cookies.set(cookie["name"], cookie["value"],
                                domain=cookie["domain"], path=cookie.get("path", "/"))


def login_in_browser(page, context, session, PlaywrightTimeoutError):
    """Sign in through the site's normal ARM form, allowing its JS fields to initialize."""
    login_url = core.LOGIN_URL
    for attempt in range(1, 4):
        page.goto(login_url, wait_until="domcontentloaded", timeout=45000)
        if "503 service unavailable" not in page.title().lower():
            break
        emit("login_page_unavailable", attempt=attempt, title=page.title())
        if attempt < 3:
            page.wait_for_timeout(15000)
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except PlaywrightTimeoutError:
        pass
    page.wait_for_timeout(800)

    initial_body = core.clean_text(page.locator("body").inner_text(timeout=10000))
    if "spam detected" in initial_body.lower():
        raise RuntimeError("TopDJMixes shows Spam detected before form submission; no credentials were submitted.")

    user_field = page.locator('input[name="user_login"]:visible').last
    pass_field = page.locator('input[name="user_pass"]:visible').last
    for _ in range(9):
        if user_field.count() and pass_field.count():
            break
        page.wait_for_timeout(5000)
    if not user_field.count() or not pass_field.count():
        body = core.clean_text(page.locator("body").inner_text(timeout=10000))
        emit("login_form_missing", title=page.title(), path=urlparse(page.url).path,
             body_excerpt=body[:350], credentials_submitted=False)
        raise RuntimeError("TopDJMixes did not present its normal login form; stopped before submitting credentials.")

    form = pass_field.locator("xpath=ancestor::form[1]")
    try:
        form.locator('input#arm_form_submission_key[name]:not([name=""])').wait_for(
            state="attached", timeout=15000)
    except PlaywrightTimeoutError:
        emit("login_form_not_initialized", title=page.title(), form_id=form.get_attribute("id") or "")
        raise RuntimeError("ARM Membership did not initialize its dynamic submission key; no login was submitted.")

    honeypot = form.locator('input[name="arm_filter_input"]')
    if honeypot.count() and (honeypot.first.input_value() or "").strip():
        raise RuntimeError("ARM honeypot is not empty; refusing to submit the login form.")

    user_field.click()
    user_field.press_sequentially(USER, delay=30)
    pass_field.click()
    pass_field.press_sequentially(PASSWORD, delay=30)
    page.wait_for_timeout(400)
    submit = form.locator('button[type="submit"]:visible, input[type="submit"]:visible, button:not([type]):visible')
    if not submit.count():
        raise RuntimeError("TopDJMixes login submit control is missing; no login request was made.")
    submit.last.click()
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except PlaywrightTimeoutError:
        pass
    page.wait_for_timeout(1000)

    body = core.clean_text(page.locator("body").inner_text(timeout=10000))
    lowered = body.lower()
    auth_cookies = sorted({c["name"] for c in context.cookies()
                           if c["name"].startswith(("wordpress_logged_in_", "wordpress_sec_"))})
    logged_out_visible = page.get_by_text(re.compile(r"\blog ?out\b", re.I)).count() > 0
    login_form_visible = page.locator('input[name="user_login"]:visible').count() > 0
    emit("test_feed_login_result", title=page.title(), path=urlparse(page.url).path,
         auth_cookie_names=auth_cookies, logged_out_visible=logged_out_visible,
         login_form_visible=login_form_visible,
         spam_detected="spam detected" in lowered,
         invalid_credentials_marker=any(marker in lowered for marker in (
             "incorrect password", "invalid username", "invalid password",
             "username or password is incorrect", "too many login attempts")))
    if "spam detected" in lowered:
        raise RuntimeError("TopDJMixes rejected this one normal login submission as Spam detected; stopping.")
    if not auth_cookies or (login_form_visible and not logged_out_visible):
        raise RuntimeError("TopDJMixes did not confirm an authenticated session.")
    transfer_cookies(context, session)


def click_download_and_resolve_audio(page, context, session, item, PlaywrightTimeoutError):
    """Click the actual episode Download button and inspect redirects without fetching the MP3 body."""
    try:
        page.goto(item["page"], wait_until="domcontentloaded", timeout=45000)
    except Exception as exc:
        emit("episode_browser_navigation_error", title=item["title"], page=safe_url(item["page"]),
             error=type(exc).__name__)
        return None, None
    try:
        page.locator("a.wpdm-download-link.download-on-click:visible").wait_for(
            state="attached", timeout=12000)
    except PlaywrightTimeoutError:
        pass
    page.wait_for_timeout(350)
    body = core.clean_text(page.locator("body").inner_text(timeout=10000)).lower()
    if "please login to download or create a free account" in body:
        emit("episode_browser_login_gate", title=item["title"], page=safe_url(item["page"]))
        return None, None

    button = page.locator("a.wpdm-download-link.download-on-click:visible").first
    if not button.count():
        emit("episode_download_button_missing", title=item["title"], page=safe_url(item["page"]))
        return None, None

    captured = []
    redirect_trace = []
    def route_download(route):
        request = route.request
        parsed = urlparse(request.url)
        if parsed.netloc.lower() in ("www.topdjmixes.com", "topdjmixes.com") and parsed.path.startswith("/download/"):
            current_url = request.url
            previous_url = request.url
            headers = dict(request.headers)
            # Ensure the authenticated browser cookies are carried into the individual
            # redirect-header requests, including HttpOnly cookies.
            try:
                browser_cookies = context.cookies("https://www.topdjmixes.com")
                cookie_values = {c["name"]: c["value"] for c in browser_cookies}
                existing = headers.get("cookie", headers.get("Cookie", ""))
                for pair in existing.split(";"):
                    if "=" in pair:
                        name, value = pair.split("=", 1)
                        cookie_values.setdefault(name.strip(), value.strip())
                if cookie_values:
                    headers["cookie"] = "; ".join(f"{k}={v}" for k, v in cookie_values.items())
            except Exception:
                pass

            for hop in range(1, 7):
                if core.is_mp3(current_url):
                    captured.append(current_url)
                    break
                response = None
                try:
                    hop_headers = dict(headers)
                    if previous_url:
                        hop_headers["referer"] = previous_url
                    response = route.fetch(url=current_url, headers=hop_headers,
                                           max_redirects=0, timeout=15000)
                    status = response.status
                    response_headers = response.headers
                    location = response_headers.get("location", "")
                    content_type = response_headers.get("content-type", "").split(";", 1)[0].lower()
                    redirect_trace.append({"hop": hop, "status": status, "url": safe_url(response.url),
                                           "content_type": content_type, "location_present": bool(location)})
                    set_cookie = response_headers.get("set-cookie", "")
                    if set_cookie:
                        try:
                            jar = SimpleCookie()
                            jar.load(set_cookie)
                            values = {}
                            existing = hop_headers.get("cookie", "")
                            for pair in existing.split(";"):
                                if "=" in pair:
                                    n, v = pair.split("=", 1)
                                    values[n.strip()] = v.strip()
                            for n, morsel in jar.items():
                                values[n] = morsel.value
                            headers["cookie"] = "; ".join(f"{n}={v}" for n, v in values.items())
                        except Exception:
                            pass
                    if status in (301, 302, 303, 307, 308) and location:
                        next_url = urljoin(response.url, location)
                        if core.is_mp3(next_url):
                            captured.append(next_url)
                            break
                        previous_url, current_url = response.url, next_url
                        continue
                    # The last URL must arrive in a Location header before this code continues.
                    # Do not fetch a response body served at a non-MP3 URL.
                    break
                except Exception as exc:
                    redirect_trace.append({"hop": hop, "error": type(exc).__name__,
                                           "url": safe_url(current_url)})
                    break
                finally:
                    if response is not None:
                        try:
                            response.dispose()
                        except Exception:
                            pass
            route.abort()
            return
        if core.is_mp3(request.url) or parsed.netloc.lower() == "files.topdjmixes.com":
            captured.append(request.url)
            route.abort()
            return
        route.continue_()

    click_error = None
    context.route("**/*", route_download)
    try:
        button.click(timeout=10000)
        page.wait_for_timeout(500)
    except Exception as exc:
        # Aborting the download navigation can surface as an expected click/navigation error.
        click_error = type(exc).__name__
    finally:
        try:
            context.unroute("**/*", route_download)
        except Exception:
            pass

    unique_urls = list(dict.fromkeys(captured))
    if not unique_urls:
        emit("episode_download_click_no_mp3", title=item["title"], page=safe_url(item["page"]),
             click_error=click_error, redirect_trace=redirect_trace[:8])
        return None, None
    audio_url = unique_urls[0]
    checked = core.validate_audio_url(session, audio_url, referer=item["page"])
    final = urlparse(checked.get("url", audio_url))
    emit("episode_audio_range_check", title=item["title"], page=safe_url(item["page"]),
         audio_url=safe_url(audio_url), valid=checked.get("valid"),
         status=checked.get("status"), content_type=checked.get("content_type"),
         content_range=checked.get("content_range"), length=checked.get("length"),
         final_host=final.netloc, final_path=final.path,
         final_query_present=bool(final.query), click_error=click_error,
         redirect_trace=redirect_trace[:8])
    if not checked.get("valid") or not core.is_mp3(checked.get("url", audio_url)):
        return None, None
    return checked["url"], checked.get("length")


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


# Test-feed generation is isolated from the production RSS.
def main():
    if not USER or not PASSWORD:
        raise RuntimeError("SOURCE_USERNAME and SOURCE_PASSWORD secrets are required.")
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
    except ImportError as exc:
        raise RuntimeError("Playwright is required for authenticated Download-button clicks.") from exc

    session = requests.Session()
    session.headers.update(core.HEADERS)
    core.configure_retries(session)

    # Keep the authenticated browser open for every real per-episode Download click.
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        context = browser.new_context(user_agent=core.HEADERS["User-Agent"], accept_downloads=False)
        page = context.new_page()
        try:
            login_in_browser(page, context, session, PlaywrightTimeoutError)
            posts = wordpress_posts(session)
            emit("wordpress_post_candidates", total=len(posts))
            candidates = inspect_candidates(session, posts)
            if len(candidates) < TARGET_ITEMS:
                raise RuntimeError(
                    f"Only {len(candidates)} TopDJMixes Download-enabled episode pages were found; need {TARGET_ITEMS}.")

            candidates.sort(key=lambda item: item["date"], reverse=True)
            result = []
            click_failures = 0
            for item in candidates:
                audio, length = click_download_and_resolve_audio(
                    page, context, session, item, PlaywrightTimeoutError)
                if audio:
                    item["audio"] = audio
                    item["audio_length"] = length or 0
                    result.append(item)
                else:
                    click_failures += 1
                if len(result) >= TARGET_ITEMS:
                    break
                time.sleep(PAUSE)
            if len(result) < TARGET_ITEMS:
                raise RuntimeError(
                    f"Only {len(result)} episodes yielded direct MP3 URLs from actual Download-button clicks; need {TARGET_ITEMS}.")
            result.sort(key=lambda item: item["date"], reverse=True)
            apply_schedule_covers(result, session, page)
            write_feed(result)
            validate_feed(result)
            emit("test_feed_written", file=str(OUT), episodes=len(result),
                 missing_covers=sum(1 for item in result if not item.get("cover")),
                 missing_description=sum(1 for item in result if not item.get("description")),
                 download_click_failures=click_failures,
                 newest_title=result[0]["title"], newest_date=result[0]["date"].isoformat(),
                 newest_audio_host=urlparse(result[0]["audio"]).netloc)
        finally:
            browser.close()

if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        emit("fatal", error=f"{type(exc).__name__}: {exc}")
        sys.exit(1)
