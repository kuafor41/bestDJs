#!/usr/bin/env python3
"""Build a direct-audio RSS feed from an authenticated TopDJMixes account."""
from __future__ import annotations

import html
import os
import re
import sys
import time
import json
import logging
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse, unquote

import requests
from bs4 import BeautifulSoup

BASE = "https://www.topdjmixes.com"
SHOWS_URL = BASE + "/mix-shows/"
LOGIN_URL = BASE + "/login/?redirect_to=" + requests.utils.quote(SHOWS_URL, safe="")
OUT = Path("public/rss.xml")
USER = os.environ.get("SOURCE_USERNAME", "").strip()
PASSWORD = os.environ.get("SOURCE_PASSWORD", "")
MAX_EPISODES = int(os.environ.get("MAX_EPISODES", "100"))
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

def log(event: str, **fields):
    print(json.dumps({"event": event, **fields}, sort_keys=True), flush=True)

def absolute(url: str, base: str = BASE) -> str:
    return urljoin(base, html.unescape(url.strip()))

def is_mp3(url: str) -> bool:
    p = urlparse(url)
    return p.scheme in ("http", "https") and ".mp3" in p.path.lower()

def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()

def form_payload(form):
    payload = {}
    for item in form.select("input[name], select[name], textarea[name], button[name]"):
        name = item.get("name")
        if not name:
            continue
        typ = (item.get("type") or "").lower()
        if typ in ("submit", "button", "image", "file", "reset"):
            continue
        if typ in ("checkbox", "radio") and not item.has_attr("checked"):
            continue
        payload[name] = item.get("value", "")
    return payload

def authenticate(session: requests.Session):
    login_page = session.get(LOGIN_URL, timeout=35)
    log("login_page", status=login_page.status_code, final_path=urlparse(login_page.url).path,
        content_type=login_page.headers.get("Content-Type", "").split(";")[0])
    login_page.raise_for_status()
    soup = BeautifulSoup(login_page.text, "html.parser")
    forms = soup.find_all("form")
    def field_hint(node):
        parts = [
            node.get("name", ""), node.get("id", ""), node.get("type", ""),
            node.get("placeholder", ""), node.get("autocomplete", ""),
            node.get("aria-label", ""),
        ]
        node_id = node.get("id")
        if node_id:
            label = soup.find("label", attrs={"for": node_id})
            if label:
                parts.append(label.get_text(" ", strip=True))
        return " ".join(parts).lower()

    candidates = []
    for form in forms:
        fields = form.select("input, select, textarea")
        passwords = [x for x in fields if (x.get("type") or "").lower() == "password"]
        usernames = [
            x for x in fields
            if (x.get("type") or "text").lower() in ("text", "email", "tel")
            and (x.get("name") or "").strip()
            and not any(token in field_hint(x) for token in ("search", "coupon", "comment"))
        ]
        if passwords and usernames:
            # Prefer a field explicitly marked as a username/email, otherwise the first
            # eligible text/email field in the same form.
            usernames.sort(key=lambda x: (
                not any(token in field_hint(x) for token in ("user", "login", "email", "account")),
                fields.index(x),
            ))
            candidates.append((form, usernames[0], passwords[0]))

    if not candidates:
        diagnostics = []
        for index, form in enumerate(forms, 1):
            fields = []
            for node in form.select("input, select, textarea, button"):
                fields.append({
                    "tag": node.name,
                    "type": node.get("type", ""),
                    "name": node.get("name", ""),
                    "id": node.get("id", ""),
                    "autocomplete": node.get("autocomplete", ""),
                    "placeholder": node.get("placeholder", ""),
                })
            diagnostics.append({
                "index": index,
                "id": form.get("id", ""),
                "class": " ".join(form.get("class", [])),
                "action_path": urlparse(absolute(form.get("action") or login_page.url, login_page.url)).path,
                "fields": fields,
            })
        log("login_form_diagnostics", form_count=len(forms), forms=diagnostics,
            page_input_count=len(soup.select("input, select, textarea")))
        raise RuntimeError("Could not identify a form containing both a password and username/email field.")

    form, user_field, pass_field = candidates[0]
    payload = form_payload(form)
    names = set(payload)
    user_key = user_field.get("name")
    pass_key = pass_field.get("name")
    if not user_key or not pass_key:
        raise RuntimeError("Detected login fields without POST names; sanitized form diagnostics required.")
    payload[user_key] = USER
    payload[pass_key] = PASSWORD
    for key in ("rememberme", "remember", "arm_rememberme"):
        if key in names:
            payload[key] = "1"
    action = absolute(form.get("action") or login_page.url, login_page.url)
    headers = dict(HEADERS)
    headers.update({"Referer": login_page.url, "Origin": BASE})
    response = session.post(action, data=payload, headers=headers, allow_redirects=True, timeout=45)
    log("login_submit", status=response.status_code, final_path=urlparse(response.url).path,
        form_action_path=urlparse(action).path, form_count=len(forms),
        field_names=sorted(names), response_type=response.headers.get("Content-Type", "").split(";")[0],
        logged_in_cookie=any(c.name.startswith("wordpress_logged_in") for c in session.cookies),
        cookie_names=sorted({c.name for c in session.cookies}))
    response.raise_for_status()
    # Some membership plugins use AJAX to establish the session. A successful redirect alone
    # is not proof: verify access to the member-only mix-show page.
    probe = session.get(SHOWS_URL, headers={"Referer": response.url}, timeout=45)
    text = probe.text.lower()
    denied = any(marker in text for marker in (
        "please login to download", "you must be logged in", "log in to access",
        "you need to be logged in", "arm_form", "name=\"user_pass\"",
    ))
    log("auth_probe", status=probe.status_code, final_path=urlparse(probe.url).path,
        logged_in_cookie=any(c.name.startswith("wordpress_logged_in") for c in session.cookies),
        access_denied_marker=denied, page_title=clean_text(BeautifulSoup(probe.text, "html.parser").title.get_text(" ", strip=True))
        if BeautifulSoup(probe.text, "html.parser").title else "")
    probe.raise_for_status()
    if denied or (urlparse(probe.url).path.rstrip("/") == "/login"):
        raise RuntimeError("Account session did not pass the member-page access check.")
    return probe

def extract_mixshow_links(soup: BeautifulSoup, page_url: str):
    """Find mixshow archive links such as /future-sound-of-egypt/."""
    excluded = {
        "mix-shows", "login", "register", "news", "music-player", "linklists",
        "schedule", "contact", "privacy-policy", "terms", "gdpr", "my-account",
        "wp-admin", "wp-content", "wp-json", "category", "tag", "page", "author",
        "search", "feed", "lost-password",
    }
    found = {}
    for a in soup.select("a[href]"):
        href = absolute(a.get("href", ""), page_url)
        parsed = urlparse(href)
        if parsed.netloc.lower() not in ("www.topdjmixes.com", "topdjmixes.com"):
            continue
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) != 1:
            continue
        slug = parts[0].lower()
        if slug in excluded or re.search(r"\\d", slug) or slug.startswith(("wp-", "page-")):
            continue
        title = clean_text(a.get_text(" ", strip=True)) or clean_text(a.get("title", ""))
        if title and len(title) > 1:
            found[href.split("#")[0].rstrip("/") + "/"] = title
    return found

def extract_show_links(soup: BeautifulSoup, page_url: str):
    found = {}
    for a in soup.select("a[href]"):
        href = absolute(a.get("href", ""), page_url)
        parsed = urlparse(href)
        if parsed.netloc.lower() not in ("www.topdjmixes.com", "topdjmixes.com"):
            continue
        path = parsed.path.rstrip("/") + "/"
        if re.fullmatch(r"/(?:resident|enhanced-sessions|global-dj-broadcast|balance|transitions|mix-show-[^/]+)-?\d+/", path, re.I) or re.fullmatch(r"/[a-z0-9-]+-\d+/", path, re.I):
            if any(x in path.lower() for x in ("/login", "/register", "/category", "/tag", "/page/")):
                continue
            title = clean_text(a.get_text(" ", strip=True)) or clean_text(a.get("title", ""))
            if title and len(title) > 2:
                found[href.split("#")[0]] = title
    return found

def find_audio(soup: BeautifulSoup, page_url: str):
    candidates = []
    for node in soup.select("audio[src], audio source[src], a[href], source[src], meta[content]"):
        url = node.get("src") or node.get("href") or ""
        if node.name == "meta":
            if node.get("property") in ("og:audio", "og:audio:secure_url") or node.get("name") in ("twitter:player:stream",):
                url = node.get("content", "")
        if not url:
            continue
        url = absolute(url, page_url)
        if is_mp3(url):
            candidates.append(url)
    # Prefer the files host, and preserve the exact encoded URL.
    candidates = list(dict.fromkeys(candidates))
    candidates.sort(key=lambda u: ("files.topdjmixes.com" not in urlparse(u).netloc, len(u)))
    return candidates[0] if candidates else None

def parse_date(soup: BeautifulSoup):
    candidates = []
    for node in soup.select("time[datetime], meta[property='article:published_time'], meta[name='date'], meta[itemprop='datePublished']"):
        value = node.get("datetime") or node.get("content")
        if value:
            candidates.append(value.strip())
    for value in candidates:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except ValueError:
            pass
    return datetime.now(timezone.utc)

def fetch_episode(session, url, fallback_title):
    r = session.get(url, headers={"Referer": SHOWS_URL}, timeout=45)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    title = clean_text(soup.title.get_text(" ", strip=True)) if soup.title else fallback_title
    title = re.sub(r"\s*[|–-]\s*TopDJMixes.*$", "", title, flags=re.I).strip() or fallback_title
    audio = find_audio(soup, r.url)
    if not audio:
        return None
    description = ""
    for selector in ("meta[name='description']", "meta[property='og:description']"):
        node = soup.select_one(selector)
        if node and node.get("content"):
            description = clean_text(node["content"])
            break
    return {"title": title, "page": url, "audio": audio, "date": parse_date(soup), "description": description}

def xml_escape(s):
    return html.escape(str(s or ""), quote=True)

def write_rss(items):
    OUT.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">',
        '<channel>',
        '<title>bestDJs Podcast</title>',
        '<link>https://www.topdjmixes.com/mix-shows/</link>',
        '<description>TopDJMixes mix shows with direct MP3 enclosure URLs.</description>',
        '<language>en-us</language>',
        f'<lastBuildDate>{format_datetime(now, usegmt=True)}</lastBuildDate>',
    ]
    for item in items:
        lines.extend([
            '<item>',
            f'<title>{xml_escape(item["title"])}</title>',
            f'<link>{xml_escape(item["page"])}</link>',
            f'<guid isPermaLink="true">{xml_escape(item["page"])}</guid>',
            f'<pubDate>{format_datetime(item["date"], usegmt=True)}</pubDate>',
            f'<description>{xml_escape(item["description"] or item["title"])}</description>',
            f'<enclosure url="{xml_escape(item["audio"])}" length="0" type="audio/mpeg"/>',
            '<itunes:explicit>no</itunes:explicit>',
            '</item>',
        ])
    lines.extend(['</channel>', '</rss>', ''])
    OUT.write_text("\n".join(lines), encoding="utf-8")

def main():
    if not USER or not PASSWORD:
        raise RuntimeError("SOURCE_USERNAME and SOURCE_PASSWORD GitHub Actions secrets are required.")
    session = requests.Session()
    session.headers.update(HEADERS)
    listing = authenticate(session)
    catalog_soup = BeautifulSoup(listing.text, "html.parser")
    show_links = extract_mixshow_links(catalog_soup, listing.url)
    # The catalog body can be JS-rendered or sparse in plain HTTP responses. The login
    # page footer contains a second directory of mixshow links, so use it as a fallback.
    if not show_links:
        fallback = session.get(LOGIN_URL, headers={"Referer": SHOWS_URL}, timeout=35)
        fallback.raise_for_status()
        show_links = extract_mixshow_links(BeautifulSoup(fallback.text, "html.parser"), fallback.url)
        log("catalog_fallback", status=fallback.status_code,
            content_length=len(fallback.text), title=clean_text(BeautifulSoup(fallback.text, "html.parser").title.get_text(" ", strip=True))
            if BeautifulSoup(fallback.text, "html.parser").title else "")
    log("shows_discovered", count=len(show_links), sample=list(show_links.items())[:5],
        catalog_html_length=len(listing.text))
    if not show_links:
        raise RuntimeError("No mixshow archive links discovered in the catalog or login-page directory.")

    # Each mixshow archive lists its own episode pages. Sample the newest four links
    # from each archive so one show cannot monopolize the feed.
    episode_links = {}
    max_shows = int(os.environ.get("MAX_SHOWS", "250"))
    per_show = int(os.environ.get("EPISODES_PER_SHOW", "4"))
    for show_index, (show_url, show_title) in enumerate(show_links.items(), 1):
        if show_index > max_shows:
            break
        try:
            show_response = session.get(show_url, headers={"Referer": SHOWS_URL}, timeout=35)
            show_response.raise_for_status()
            show_soup = BeautifulSoup(show_response.text, "html.parser")
            found = extract_show_links(show_soup, show_response.url)
            for episode_url, episode_title in list(found.items())[:per_show]:
                episode_links.setdefault(episode_url, episode_title)
            if show_index % 25 == 0:
                log("show_progress", checked=show_index, episode_pages=len(episode_links))
        except (requests.RequestException, ValueError) as exc:
            log("show_error", url=show_url, error=type(exc).__name__)
        time.sleep(0.08)
    log("episode_pages_discovered", count=len(episode_links), sample=list(episode_links.items())[:5])
    if not episode_links:
        raise RuntimeError("No episode pages were discovered inside the mixshow archive pages.")

    episodes = []
    errors = 0
    for index, (url, title) in enumerate(episode_links.items(), 1):
        try:
            item = fetch_episode(session, url, title)
            if item:
                episodes.append(item)
        except (requests.RequestException, ValueError) as exc:
            errors += 1
            log("episode_error", url=url, error=type(exc).__name__)
        if index % 10 == 0:
            log("progress", checked=index, found=len(episodes), errors=errors)
        if len(episodes) >= MAX_EPISODES:
            break
        time.sleep(0.15)
    if not episodes:
        raise RuntimeError("Episode pages were discovered, but no direct MP3 URLs were exposed to the crawler. Check login/download authorization or the Download-button redirect.")
    episodes.sort(key=lambda x: x["date"], reverse=True)
    write_rss(episodes)
    log("feed_written", file=str(OUT), episodes=len(episodes), errors=errors,
        newest=episodes[0]["title"], newest_audio_host=urlparse(episodes[0]["audio"]).netloc)
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
            f.write(f"episode_count={len(episodes)}\n")

if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log("fatal", error=f"{type(exc).__name__}: {exc}")
        sys.exit(1)
