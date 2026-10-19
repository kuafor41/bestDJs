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
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
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

def configure_retries(session: requests.Session):
    retry = Retry(
        total=4, connect=4, read=3, status=4,
        backoff_factor=1.2,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "HEAD"}),
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=20, pool_maxsize=20)
    session.mount("https://", adapter)
    session.mount("http://", adapter)

def validate_audio_url(session: requests.Session, url: str, referer: str = SHOWS_URL):
    """Validate an MP3 with a one-byte Range request; never fetch the full audio body."""
    response = None
    try:
        response = session.get(
            url,
            headers={**HEADERS, "Referer": referer, "Range": "bytes=0-0"},
            allow_redirects=True, stream=True, timeout=(15, 30),
        )
        final_url = response.url
        content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        content_range = response.headers.get("Content-Range", "")
        content_length = response.headers.get("Content-Length", "")
        total = None
        match = re.search(r"/(\d+|\*)$", content_range)
        if match and match.group(1) != "*":
            total = int(match.group(1))
        elif content_length.isdigit() and response.status_code == 200:
            total = int(content_length)
        valid = (
            response.status_code in (200, 206)
            and (content_type.startswith("audio/") or is_mp3(final_url))
            and not urlparse(final_url).path.lower().endswith((".html", ".htm"))
        )
        return {
            "valid": valid, "url": final_url, "length": total,
            "status": response.status_code, "content_type": content_type,
            "content_range": content_range,
        }
    except requests.RequestException as exc:
        return {"valid": False, "url": url, "length": None,
                "status": None, "error": type(exc).__name__}
    finally:
        if response is not None:
            response.close()

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
    """Use Chromium for the membership login, then share its cookies with requests."""
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
    except ImportError as exc:
        raise RuntimeError("Playwright is required to authenticate with TopDJMixes.") from exc

    sample_url = BASE + "/the-midnight-project-228/"
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = browser.new_context(user_agent=HEADERS["User-Agent"])
        page = context.new_page()
        try:
            # The host may briefly return 503 during bursts. Retry the login page
            # slowly and stop after a small number of attempts rather than hammering it.
            for login_attempt in range(1, 4):
                page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=45000)
                if "503 service unavailable" not in page.title().lower():
                    break
                log("login_page_unavailable", attempt=login_attempt, title=page.title())
                if login_attempt < 3:
                    page.wait_for_timeout(20000)
            if "503 service unavailable" in page.title().lower():
                raise RuntimeError("TopDJMixes login page continued returning 503 after slow retries.")
            # Let ARM Membership's browser-side form initialization and nonce/session AJAX finish
            # before selecting fields. A domcontentloaded-only submit can trip its anti-spam checks.
            try:
                page.wait_for_load_state("networkidle", timeout=20000)
            except PlaywrightTimeoutError:
                pass
            page.wait_for_timeout(1000)
            pre_submit_text = clean_text(page.locator("body").inner_text(timeout=10000))
            if "spam detected" in pre_submit_text.lower():
                log("browser_login_preexisting_block", title=page.title(),
                    path=urlparse(page.url).path, spam_detected_before_submit=True,
                    body_excerpt=pre_submit_text[:400])
                raise RuntimeError("TopDJMixes already displays Spam detected before this login submission; no credentials were submitted.")
            # Some WordPress themes include a hidden header login form before the
            # visible page form. Select visible controls so we submit the actual form.
            user_field = page.locator('input[name="user_login"]:visible').last
            pass_field = page.locator('input[name="user_pass"]:visible').last
            # A Cloudflare/security interstitial can temporarily replace the login form.
            # Wait for the normal login UI; do not submit anything while it is absent.
            for form_wait in range(1, 10):
                if user_field.count() > 0 and pass_field.count() > 0:
                    break
                if form_wait < 9:
                    page.wait_for_timeout(5000)
            if user_field.count() == 0 or pass_field.count() == 0:
                challenge_body = clean_text(page.locator("body").inner_text(timeout=10000))
                challenge_markers = {
                    "one_moment": "one moment, please" in challenge_body.lower(),
                    "captcha": "captcha" in challenge_body.lower(),
                    "verify": "verify you are human" in challenge_body.lower(),
                    "cloudflare": "cloudflare" in challenge_body.lower(),
                }
                log("browser_login_form", username_visible_count=page.locator('input[name="user_login"]:visible').count(),
                    password_visible_count=page.locator('input[name="user_pass"]:visible').count(),
                    username_total_count=page.locator('input[name="user_login"]').count(),
                    password_total_count=page.locator('input[name="user_pass"]').count(),
                    title=page.title(), path=urlparse(page.url).path,
                    challenge_markers=challenge_markers, body_excerpt=challenge_body[:450],
                    credentials_submitted=False)
                raise RuntimeError("TopDJMixes did not expose its normal login form after waiting; stopped before submitting credentials.")

            form = pass_field.locator("xpath=ancestor::form[1]")
            # ARM Membership adds this key dynamically and refreshes its nonce by AJAX.
            # Do not submit until that same visible form has a named submission key.
            try:
                form.locator('input#arm_form_submission_key[name]:not([name=""])').wait_for(
                    state="attached", timeout=15000
                )
            except PlaywrightTimeoutError:
                readiness = form.evaluate("(f) => ({id:f.id,random_id:f.getAttribute('data-random-id'),"
                    "submission_key:f.querySelector('#arm_form_submission_key')?.name||'',"
                    "fields:Array.from(f.querySelectorAll('input[name],input#arm_form_submission_key')).map(e=>"
                    "({name:e.name||'',id:e.id||'',type:e.type||'',value_present:!!e.value})).slice(0,30)})")
                log("browser_login_form_not_ready", form_state=readiness,
                    title=page.title(), path=urlparse(page.url).path)
                raise RuntimeError("ARM Membership did not initialize its anti-spam submission key; login was not submitted.")
            form_info = form.evaluate("(f) => ({action: f.action, method: f.method, id: f.id,"
                "className: String(f.className).slice(0,160),random_id:f.getAttribute('data-random-id'),"
                "submission_key_name:f.querySelector('#arm_form_submission_key')?.name||'',"
                "submission_key_ready:!!(f.querySelector('#arm_form_submission_key')?.name),"
                "honeypot_empty:!((f.querySelector('input[name=arm_filter_input]')?.value||'').trim()),"
                "fields: Array.from(f.querySelectorAll('input,button,select,textarea')).map(e => "
                "({tag:e.tagName.toLowerCase(),name:e.name||'',type:e.type||'',id:e.id||'',"
                "visible:!!(e.offsetWidth||e.offsetHeight||e.getClientRects().length),"
                "required:!!e.required,autocomplete:e.autocomplete||''})).slice(0,40)})")
            honeypot = form.locator('input[name="arm_filter_input"]')
            if honeypot.count() and (honeypot.first.input_value() or "").strip():
                log("browser_login_honeypot_not_empty", path=urlparse(page.url).path)
                raise RuntimeError("ARM Membership honeypot was unexpectedly populated; login was not submitted.")
            # Use actual keyboard events instead of fill(), which skips keypress/keydown.
            # The ARM form's client-side anti-spam handler observes real typing.
            user_field.click()
            user_field.press_sequentially(USER, delay=35)
            pass_field.click()
            pass_field.press_sequentially(PASSWORD, delay=35)
            page.wait_for_timeout(500)
            submit = form.locator('button[type="submit"]:visible, input[type="submit"]:visible, button:not([type]):visible')
            if submit.count():
                submit.last.click()
            else:
                # Native submission respects the form's own submit handler.
                form.evaluate("(f) => f.requestSubmit()")
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except PlaywrightTimeoutError:
                pass
            page.wait_for_timeout(1500)

            login_body_raw = page.locator("body").inner_text(timeout=10000)
            login_body = login_body_raw.lower()
            form_messages = form.locator(".arm_error_msg, .arm_error, .error, .alert, [role=alert], .arm_success_msg").all_inner_texts()
            log("browser_login_response", body_excerpt=clean_text(login_body_raw)[:700],
                form_messages=[clean_text(x)[:180] for x in form_messages[:10]],
                form_visible=form.is_visible(),
                submit_buttons=form.locator('button:visible, input[type="submit"]:visible, input[type="button"]:visible').evaluate_all(
                    "(els) => els.map(e => ({tag:e.tagName.toLowerCase(),type:e.type||'',name:e.name||'',id:e.id||'',text:(e.innerText||e.value||'').trim().slice(0,100),disabled:!!e.disabled})).slice(0,15)"))
            invalid = any(marker in login_body for marker in (
                "incorrect password", "invalid username", "invalid password",
                "username or password is incorrect", "too many login attempts",
            ))
            spam_detected = "spam detected" in login_body
            log("browser_login_block_status", spam_detected=spam_detected,
                invalid_credentials_marker=invalid, response_path=urlparse(page.url).path)
            login_form_remains = page.locator('input[name="user_login"]:visible').count() > 0
            logout_visible = page.get_by_text(re.compile(r"\blog ?out\b", re.I)).count() > 0
            auth_cookie_names = sorted({c["name"] for c in context.cookies()
                                        if c["name"].startswith(("wordpress_logged_in_", "wordpress_sec_"))})
            log("browser_login_result", path=urlparse(page.url).path, title=page.title(),
                form_action=form_info["action"], form_method=form_info["method"],
                form_id=form_info["id"], form_fields=form_info.get("fields", []), invalid_credentials_marker=invalid,
                login_form_still_visible=login_form_remains, logout_text_visible=logout_visible,
                wordpress_auth_cookie_names=auth_cookie_names,
                cookie_names=sorted({c["name"] for c in context.cookies()}))
            if spam_detected:
                raise RuntimeError("TopDJMixes returned Spam detected after one normal keyboard-driven form submission; stopping rather than retrying.")
            if invalid:
                raise RuntimeError("TopDJMixes returned a login failure message.")
            if login_form_remains and not auth_cookie_names and not logout_visible:
                raise RuntimeError("Login form remained visible and no WordPress authentication cookie appeared; credentials were not authenticated.")

            page.goto(sample_url, wait_until="domcontentloaded", timeout=45000)
            try:
                page.wait_for_load_state("networkidle", timeout=10000)
            except PlaywrightTimeoutError:
                pass
            page.wait_for_timeout(500)
            body = page.locator("body").inner_text(timeout=10000).lower()
            gate = "please login to download or create a free account" in body
            download_links = page.locator('a[href]').evaluate_all(
                "(els) => els.filter(a => /download/i.test((a.innerText || '') + ' ' + a.href))"
                ".map(a => ({text: (a.innerText || '').trim().slice(0,80), href: a.href}))"
            )
            log("browser_episode_probe", path=urlparse(page.url).path, title=page.title(),
                download_gate=gate, download_links=download_links[:8],
                html_length=len(page.content()),
                cookie_names=sorted({c["name"] for c in context.cookies()}))
            if gate:
                raise RuntimeError("Login did not unlock the sample episode's Download area.")

            # Inspect and safely click the Download control once. Intercept MP3/file-host
            # requests before the browser can download audio bytes; we only need its URL.
            download_controls = page.locator("a,button,input,[role='button'],[onclick],[id*='download' i],[class*='download' i]").evaluate_all(
                "(els) => els.filter(e => { const visible=!!(e.offsetWidth||e.offsetHeight||e.getClientRects().length);"
                "const text=(e.innerText||e.textContent||e.value||e.getAttribute('aria-label')||'').trim();"
                "const attrs=[e.id||'',String(e.className||''),e.getAttribute('href')||'',e.getAttribute('onclick')||''].join(' ');"
                "return visible && (/^\\s*DOWNLOAD\\s*$/i.test(text) || /download/i.test(attrs)); })"
                ".map(e => {const h=e.getAttribute('href')||''; let u=null; try{u=h?new URL(h,location.href):null;}catch(_){}"
                "return {tag:e.tagName.toLowerCase(),text:(e.innerText||e.textContent||e.value||'').trim().slice(0,100),"
                "href_host:u?u.host:'',href_path:u?u.pathname:'',href_query_present:u?!!u.search:false,"
                "id:e.id||'',className:String(e.className||'').slice(0,120),"
                "onclick:(e.getAttribute('onclick')||'').slice(0,160),type:e.type||'',visible:true}; }).slice(0,20)"
            )
            intercepted_audio = []
            related_requests = []
            related_responses = []
            related_response_objects = []
            audio_candidates = []
            click_target = None
            def route_probe(route):
                request_url = route.request.url
                parsed_url = urlparse(request_url)
                # Abort audio requests before the browser can fetch the MP3 body.
                if is_mp3(request_url) or parsed_url.netloc.lower() == "files.topdjmixes.com":
                    intercepted_audio.append(request_url)
                    route.abort()
                else:
                    route.continue_()
            def request_probe(request):
                parsed_url = urlparse(request.url)
                # Capture request metadata for this click only; never log cookies or headers.
                if len(related_requests) < 100:
                    related_requests.append({"method": request.method, "host": parsed_url.netloc,
                                             "path": parsed_url.path, "resource_type": request.resource_type})
            def response_probe(response):
                parsed_url = urlparse(response.url)
                try:
                    headers = response.headers
                    content_type = headers.get("content-type", "")[:100]
                except Exception:
                    content_type = ""
                if (parsed_url.netloc.lower() in {"topdjmixes.com", "www.topdjmixes.com", "files.topdjmixes.com"}
                        or any(term in parsed_url.path.lower() for term in ("download", "admin-ajax", "/wp-json/", "wpdm"))):
                    if len(related_responses) < 40:
                        related_responses.append({"status": response.status, "host": parsed_url.netloc,
                                                  "path": parsed_url.path, "content_type": content_type,
                                                  "resource_type": response.request.resource_type})
                # Save only small text/XHR responses for later inspection. Audio and binary
                # response bodies are never read here.
                if (len(related_response_objects) < 30
                        and response.request.resource_type in ("xhr", "fetch")
                        and any(term in content_type.lower() for term in ("json", "text", "javascript"))):
                    related_response_objects.append(response)
            page.on("request", request_probe)
            page.on("response", response_probe)
            context.route("**/*", route_probe)
            click_error = None
            try:
                # This is the actual audio control rendered by TopDJMixes/WP Download Manager.
                download_button = page.locator("a.wpdm-download-link.download-on-click:visible").first
                if download_button.count():
                    click_target = "a.wpdm-download-link.download-on-click"
                else:
                    # Keep a narrow fallback for a changed label/markup, but never click Save Tracklist.
                    download_button = page.locator(
                        "a:visible,button:visible,[role='button']:visible,[onclick]:visible,"
                        "[id*='download' i]:visible,[class*='download' i]:visible"
                    ).filter(has_text=re.compile(r"^\\s*DOWNLOAD\\s*$", re.I)).first
                    click_target = "visible exact DOWNLOAD fallback"
                if download_button.count():
                    download_button.click(timeout=7000)
                    # Free-member download gates may include a countdown. Click once and wait
                    # for the generated file URL; the route above aborts the MP3 request itself.
                    for _ in range(80):
                        page.wait_for_timeout(500)
                        if intercepted_audio:
                            break
                        if page.locator('a[href*=".mp3" i],audio[src*=".mp3" i],source[src*=".mp3" i]').count():
                            break
                else:
                    click_error = "No visible WPDM download control or exact DOWNLOAD button found"
            except Exception as exc:
                click_error = type(exc).__name__

            # Inspect only small text/XHR responses generated by the button. The file request
            # itself is intercepted above; no audio body is downloaded.
            for response in related_response_objects[:30]:
                try:
                    ctype = response.headers.get("content-type", "").lower()
                    if not any(term in ctype for term in ("json", "text", "javascript")):
                        continue
                    if response.request.resource_type not in ("xhr", "fetch"):
                        continue
                    body_text = response.text()
                    if len(body_text) > 500000:
                        continue
                    body_text = body_text.replace("\\/", "/").replace("\\u0026", "&")
                    for raw in re.findall(r'''[^\s"'<>]+\.mp3(?:\?[^\s"'<>]*)?''', body_text, re.I):
                        raw = html.unescape(raw).strip(" \t\r\n\"'()[]{}<>,;")
                        if raw.startswith("//"):
                            raw = "https:" + raw
                        candidate = absolute(raw, response.url)
                        if is_mp3(candidate):
                            audio_candidates.append(candidate)
                except Exception:
                    continue
            audio_candidates = list(dict.fromkeys(audio_candidates))
            post_click_links = page.locator("a[href]").evaluate_all(
                "(els) => els.filter(a => /\\.mp3(?:\\?|$)/i.test(a.href) || /download/i.test(a.innerText||''))"
                ".map(a => {const u=new URL(a.href,location.href); return {text:(a.innerText||'').trim().slice(0,80),"
                "host:u.host,path:u.pathname,query_present:!!u.search,url_if_no_query:u.search?'':u.href};}).slice(0,20)"
            )
            for link in post_click_links:
                link_url = link.get("url_if_no_query")
                if link_url and is_mp3(link_url):
                    audio_candidates.append(link_url)
            audio_candidates = list(dict.fromkeys(audio_candidates))
            context.unroute("**/*", route_probe)
            page.remove_listener("request", request_probe)
            page.remove_listener("response", response_probe)
            log("browser_download_click_probe", click_target=click_target, controls=download_controls,
                related_requests=related_requests[:30], related_responses=related_responses[:30],
                post_click_links=post_click_links,
                response_audio_candidates=[
                    {"host": urlparse(u).netloc, "path": urlparse(u).path,
                     "query_present": bool(urlparse(u).query),
                     "url_if_no_query": u if not urlparse(u).query else None}
                    for u in audio_candidates[:10]
                ],
                intercepted_audio=[
                    {"host": urlparse(u).netloc, "path": urlparse(u).path,
                     "query_present": bool(urlparse(u).query),
                     "url_if_no_query": u if not urlparse(u).query else None}
                    for u in intercepted_audio[:10]
                ], click_error=click_error, final_path=urlparse(page.url).path,
                title=page.title())

            # Inspect the catalogue in the same authenticated browser that passed the episode gate.
            page.goto(SHOWS_URL, wait_until="domcontentloaded", timeout=45000)
            try:
                page.wait_for_load_state("networkidle", timeout=10000)
            except PlaywrightTimeoutError:
                pass
            page.wait_for_timeout(1000)
            browser_catalog_html = page.content()
            browser_catalog_soup = BeautifulSoup(browser_catalog_html, "html.parser")
            anchor_samples = []
            for a in browser_catalog_soup.select("a[href]"):
                label = clean_text(a.get_text(" ", strip=True))
                href = absolute(a.get("href", ""), page.url)
                if label and any(term in label.lower() for term in ("future sound", "resident", "anjunadeep", "state of trance", "mix show")):
                    parsed = urlparse(href)
                    anchor_samples.append({"text": label[:70], "host": parsed.netloc, "path": parsed.path})
                if len(anchor_samples) >= 25:
                    break
            log("browser_catalog_probe", path=urlparse(page.url).path,
                html_length=len(browser_catalog_html), anchors=len(browser_catalog_soup.select("a[href]")),
                show_anchor_samples=anchor_samples)

            for cookie in context.cookies():
                if "topdjmixes.com" in cookie.get("domain", ""):
                    session.cookies.set(cookie["name"], cookie["value"],
                                        domain=cookie["domain"], path=cookie.get("path", "/"))
        finally:
            browser.close()

    # Validate only audio URLs actually produced by the site's Download-button flow.
    # Each check is a one-byte Range request; no full MP3 is read.
    clicked_urls = list(dict.fromkeys(intercepted_audio + audio_candidates))
    for audio_url in clicked_urls[:10]:
        checked = validate_audio_url(session, audio_url, referer=sample_url)
        final = urlparse(checked.get("url", audio_url))
        original = urlparse(audio_url)
        log("download_click_audio_range_probe",
            original_host=original.netloc, original_path=original.path,
            original_query_present=bool(original.query),
            original_url_if_no_query=audio_url if not original.query else None,
            valid=checked.get("valid"), status=checked.get("status"),
            content_type=checked.get("content_type"), content_range=checked.get("content_range"),
            length=checked.get("length"), error=checked.get("error"),
            final_host=final.netloc, final_path=final.path,
            final_query_present=bool(final.query),
            final_url_if_no_query=checked.get("url") if not final.query else None)

    # Verify that the transferred HTTP session retains the same member access.
    probe = session.get(sample_url, headers={"Referer": LOGIN_URL}, timeout=45)
    probe.raise_for_status()
    probe_soup = BeautifulSoup(probe.text, "html.parser")
    probe_body = clean_text(probe_soup.get_text(" ", strip=True)).lower()
    gate = "please login to download or create a free account" in probe_body
    log("http_session_probe", status=probe.status_code, path=urlparse(probe.url).path,
        html_length=len(probe.text), download_gate=gate,
        cookie_names=sorted({c.name for c in session.cookies}))
    if gate:
        raise RuntimeError("Browser login appeared to complete, but transferred cookies do not unlock Download.")
    listing = session.get(SHOWS_URL, headers={"Referer": sample_url}, timeout=45)
    listing.raise_for_status()
    log("listing_fetch", status=listing.status_code, final_path=urlparse(listing.url).path,
        html_length=len(listing.text))
    return listing, browser_catalog_html

def extract_mixshow_links(soup: BeautifulSoup, page_url: str):
    """Find mixshow archives from root-level slugs or nested directory links."""
    excluded = {
        "mix-shows", "mixshow", "shows", "login", "register", "news", "music-player", "linklists",
        "schedule", "contact", "privacy-policy", "terms", "terms-conditions", "gdpr", "my-account",
        "membership-account", "wp-admin", "wp-content", "wp-json", "category", "tag", "page", "author",
        "search", "feed", "lost-password",
    }
    found = {}
    for a in soup.select("a[href]"):
        href = absolute(a.get("href", ""), page_url)
        parsed = urlparse(href)
        if parsed.netloc.lower() not in ("www.topdjmixes.com", "topdjmixes.com"):
            continue
        parts = [part for part in parsed.path.split("/") if part]
        if not parts:
            continue
        # Most archives use /show-name/, but support /mix-shows/show-name/ too.
        if len(parts) == 1:
            slug = parts[0].lower()
        elif len(parts) == 2 and parts[0].lower() in ("mix-shows", "mixshow", "shows"):
            slug = parts[1].lower()
        else:
            continue
        if slug in excluded or re.search(r"-\d+$", slug) or slug.startswith(("wp-", "page-")):
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
    # Page fetches are deliberately sequential; the caller pauses between episode requests.
    r = session.get(url, headers={"Referer": SHOWS_URL}, timeout=45)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    title = clean_text(soup.title.get_text(" ", strip=True)) if soup.title else fallback_title
    title = re.sub(r"\s*[|–-]\s*TopDJMixes.*$", "", title, flags=re.I).strip() or fallback_title
    audio = find_audio(soup, r.url)
    if not audio:
        # The site may expose a Download button that redirects to the protected MP3.
        for anchor in soup.select("a[href]"):
            label = clean_text(anchor.get_text(" ", strip=True)).lower()
            href = anchor.get("href", "")
            if "download" not in label and "download" not in href.lower():
                continue
            target = absolute(href, r.url)
            if urlparse(target).netloc.lower() not in (
                "www.topdjmixes.com", "topdjmixes.com", "files.topdjmixes.com"
            ):
                continue
            try:
                download_response = session.get(
                    target, headers={"Referer": r.url}, allow_redirects=True,
                    stream=True, timeout=45
                )
                final_url = download_response.url
                content_type = download_response.headers.get("Content-Type", "").lower()
                status = download_response.status_code
                download_response.close()
                if status < 400 and (is_mp3(final_url) or "audio/mpeg" in content_type):
                    audio = final_url
                    break
            except requests.RequestException:
                continue
    if not audio:
        log("episode_audio_missing", page=url, title=title)
        return None
    # A discovered URL is not enough: verify it is a playable audio response using
    # a one-byte range request. This deliberately does not download the MP3.
    checked = validate_audio_url(session, audio, referer=r.url)
    if not checked.get("valid"):
        log("episode_audio_invalid", page=url, audio_host=urlparse(audio).netloc,
            status=checked.get("status"), content_type=checked.get("content_type"),
            error=checked.get("error"))
        return None
    audio = checked["url"]
    description = ""
    for selector in ("meta[name='description']", "meta[property='og:description']"):
        node = soup.select_one(selector)
        if node and node.get("content"):
            description = clean_text(node["content"])
            break
    return {"title": title, "page": url, "audio": audio, "audio_length": checked.get("length"), "date": parse_date(soup), "description": description}

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
            f'<enclosure url="{xml_escape(item["audio"])}" length="{int(item.get("audio_length") or 0)}" type="audio/mpeg"/>',
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
    configure_retries(session)
    listing, browser_catalog_html = authenticate(session)
    # Prefer catalogue links captured from the authenticated browser, then merge HTTP links.
    browser_soup = BeautifulSoup(browser_catalog_html, "html.parser")
    catalog_soup = BeautifulSoup(listing.text, "html.parser")
    show_links = extract_mixshow_links(browser_soup, SHOWS_URL)
    browser_show_count = len(show_links)
    if len(show_links) < 5:
        http_show_links = extract_mixshow_links(catalog_soup, listing.url)
        show_links.update({u: t for u, t in http_show_links.items() if u not in show_links})
    log("catalog_parser_probe", browser_show_count=browser_show_count,
        http_show_count=len(extract_mixshow_links(catalog_soup, listing.url)),
        browser_anchor_count=len(browser_soup.select("a[href]")))
    # The catalog body can be JS-rendered or sparse in plain HTTP responses. The login
    # page footer contains a second directory of mixshow links, so use it as a fallback.
    if len(show_links) < 5:
        fallback = session.get(LOGIN_URL, headers={"Referer": SHOWS_URL}, timeout=35)
        fallback.raise_for_status()
        fallback_links = extract_mixshow_links(BeautifulSoup(fallback.text, "html.parser"), fallback.url)
        # Merge fallback links; never discard links already extracted from the authenticated browser.
        show_links.update({u: t for u, t in fallback_links.items() if u not in show_links})
        log("catalog_fallback", status=fallback.status_code,
            content_length=len(fallback.text), fallback_show_count=len(fallback_links),
            fallback_samples=list(fallback_links.items())[:10],
            title=clean_text(BeautifulSoup(fallback.text, "html.parser").title.get_text(" ", strip=True))
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
        time.sleep(float(os.environ.get("REQUEST_DELAY_SECONDS", "2.5")))
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
        time.sleep(float(os.environ.get("REQUEST_DELAY_SECONDS", "2.5")))
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
