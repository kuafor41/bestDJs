#!/usr/bin/env python3
"""Public-only discovery probe. Never logs in, submits forms, or downloads audio."""
import json
import re
from urllib.parse import urlparse
import requests
from bs4 import BeautifulSoup
import xml.etree.ElementTree as ET

S = requests.Session()
S.headers.update({
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
})
BASE = "https://www.topdjmixes.com"

def emit(event, **data):
    print(json.dumps({"event": event, **data}, ensure_ascii=False, sort_keys=True), flush=True)

def safe_url(url):
    p = urlparse(url)
    # Do not publish potential signed/tokenized URLs to Actions logs.
    return {"host": p.netloc, "path": p.path, "query_present": bool(p.query),
            "url_if_no_query": url if not p.query else None}

def get(url, timeout=25):
    try:
        r = S.get(url, timeout=timeout, allow_redirects=True)
        emit("endpoint", requested=url, status=r.status_code,
             final=safe_url(r.url), content_type=r.headers.get("content-type", ""),
             bytes=len(r.content), title=(BeautifulSoup(r.text[:300000], "html.parser").title.get_text(" ", strip=True)[:120]
                 if "html" in r.headers.get("content-type", "").lower() else ""))
        return r
    except Exception as e:
        emit("endpoint_error", requested=url, error=type(e).__name__, detail=str(e)[:160])
        return None

def read_html(url):
    r = get(url)
    if not r or r.status_code >= 400 or "html" not in r.headers.get("content-type", "").lower():
        return
    soup = BeautifulSoup(r.text, "html.parser")
    mp3s = set()
    for node in soup.select("audio[src], audio source[src], source[src], a[href], meta[content], iframe[src]"):
        value = node.get("src") or node.get("href") or node.get("content") or ""
        if ".mp3" in value.lower():
            mp3s.add(requests.compat.urljoin(r.url, value))
    scripts = []
    for node in soup.select("script[src]"):
        src = requests.compat.urljoin(r.url, node.get("src", ""))
        parsed = urlparse(src)
        name = (parsed.path + " " + parsed.query).lower()
        if parsed.netloc.endswith("topdjmixes.com") and any(t in name for t in ("arm", "member", "login", "form", "theme")):
            scripts.append(src)
    forms = []
    for form in soup.select("form"):
        fields = []
        for i in form.select("input[name],button[name],select[name],textarea[name]"):
            fields.append({"name": i.get("name", ""), "type": i.get("type", i.name),
                           "hidden": (i.get("type") or "").lower() == "hidden",
                           "value_present": bool(i.get("value"))})
        if any(any(k in f["name"].lower() for k in ("user_login", "user_pass", "arm_")) for f in fields):
            forms.append({"id": form.get("id", ""), "action": safe_url(requests.compat.urljoin(r.url, form.get("action", ""))),
                          "method": form.get("method", "get"), "fields": fields[:35]})
    emit("public_page_scan", page=safe_url(r.url), title=soup.title.get_text(" ", strip=True)[:140] if soup.title else "",
         direct_mp3_urls=[safe_url(u) for u in sorted(mp3s)[:30]], related_scripts=[safe_url(u) for u in scripts[:30]],
         member_forms=forms[:8], arm_form_submission_key_in_html="arm_form_submission_key" in r.text,
         spam_detected_in_initial_html="spam detected" in r.text.lower(),
         has_download_gate="please login to download or create a free account" in soup.get_text(" ", strip=True).lower(),
         html_length=len(r.text))
    # Read relevant public JS solely to identify normal form behavior; no form submission.
    markers = ("arm_form_submission_key", "arm_nonce_check", "spam detected", "honeypot", "j5khzvhz")
    hits = []
    for src in scripts[:8]:
        js = get(src, timeout=15)
        if not js or js.status_code >= 400 or len(js.content) > 700000:
            continue
        low = js.text.lower()
        for marker in markers:
            if marker in low:
                hits.append({"script": safe_url(js.url), "marker": marker,
                             "count": low.count(marker)})
    if hits:
        emit("public_form_js_markers", hits=hits[:40])

def parse_feed(xml_text, source):
    try:
        root = ET.fromstring(xml_text)
    except Exception as e:
        emit("xml_parse", source=source, parsed=False, error=type(e).__name__)
        return
    items = []
    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        enclosures = []
        for enc in item.findall("enclosure"):
            u = enc.attrib.get("url", "")
            if u:
                enclosures.append({"url": safe_url(u), "type": enc.attrib.get("type", ""),
                                   "length": enc.attrib.get("length", "")})
        if any(s in title.lower() for s in ("midnight project", "defected", "resident", "group therapy")):
            items.append({"title": title[:180], "enclosures": enclosures[:3],
                          "link": (item.findtext("link") or "")[:250]})
    emit("feed_scan", source=source, item_count=len(root.findall(".//item")),
         matching_items=items[:25])

# Scan two recent, public TopDJMixes episodes, not the historical test episode.
read_html(BASE + "/the-midnight-project-228/")
read_html(BASE + "/defected-in-the-house-09-10-2026/")

# Public WordPress publishing endpoints; no authentication or guessed write/action APIs.
for u in (
    BASE + "/feed/",
    BASE + "/?feed=rss2",
    BASE + "/the-midnight-project-228/feed/",
    BASE + "/wp-json/",
    BASE + "/wp-json/wp/v2/posts?slug=the-midnight-project-228",
    BASE + "/wp-sitemap.xml",
    BASE + "/sitemap_index.xml",
    BASE + "/wp-sitemap-posts-post-1.xml",
):
    r = get(u, timeout=20)
    if r and r.status_code < 400 and ("xml" in r.headers.get("content-type", "").lower()
                                      or r.text.lstrip().startswith("<?xml")):
        parse_feed(r.text[:5000000], safe_url(r.url))

# Look up the official public feed for a matching show, then extract only episode metadata.
try:
    lookup = get("https://itunes.apple.com/lookup?id=1618498106&entity=podcast", timeout=20)
    if lookup and lookup.status_code == 200:
        data = lookup.json()
        for record in data.get("results", []):
            feed_url = record.get("feedUrl")
            if not feed_url:
                continue
            emit("official_podcast_feed", title=record.get("collectionName", ""),
                 feed=safe_url(feed_url))
            f = get(feed_url, timeout=25)
            if f and f.status_code == 200:
                try:
                    root = ET.fromstring(f.content)
                    matches = []
                    for item in root.findall(".//item"):
                        title = (item.findtext("title") or "").strip()
                        if "227" in title or "228" in title:
                            enc = item.find("enclosure")
                            matches.append({"title": title,
                                            "audio": safe_url(enc.attrib.get("url", "")) if enc is not None else None,
                                            "pubDate": item.findtext("pubDate")})
                    emit("official_episode_match", feed=safe_url(f.url), matches=matches[:10])
                except Exception as e:
                    emit("official_feed_parse_error", error=type(e).__name__)
except Exception as e:
    emit("itunes_lookup_error", error=type(e).__name__, detail=str(e)[:160])
