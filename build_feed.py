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

def format_description(value: str) -> str:
    """Preserve description line breaks and put two blank lines before Tracklist."""
    text = (value or "").replace("\r\n", "\n").replace("\r", "\n")
    # Normalize spaces within each line without collapsing the line breaks themselves.
    text = "\n".join(re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n"))
    text = text.strip()
    # Exactly two empty lines (three newline characters) separate Size and Tracklist.
    text = re.sub(r"(?is)(Size:.*?)(?:\s*)(Tracklist:)", r"\1\n\n\n\2", text)
    return text

def extract_description(soup: BeautifulSoup) -> str:
    """Prefer the actual episode content so Size/Tracklist are present in RSS."""
    selectors = (
        ".entry-content", ".post-content", ".wp-block-post-content",
        ".elementor-widget-theme-post-content", "article",
    )
    for selector in selectors:
        for node in soup.select(selector):
            # Work on a copy so formatting doesn't mutate the parsed page.
            content = BeautifulSoup(str(node), "html.parser")
            for br in content.select("br"):
                br.replace_with("\n")
            for block in content.select("p, div, li, h1, h2, h3, h4, h5, h6, tr"):
                block.insert_before("\n")
                block.insert_after("\n")
            text = content.get_text()
            text = re.sub(r"\n[ \t]*\n+", "\n", text)
            text = format_description(text)
            if re.search(r"(?i)Size\s*:", text) and re.search(r"(?i)Tracklist\s*:", text):
                return text
    # Fall back to description metadata when page content has no Size/Tracklist.
    for selector in ("meta[name='description']", "meta[property='og:description']"):
        node = soup.select_one(selector)
        if node and node.get("content"):
            return format_description(node["content"])
    return ""

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
            clicked_download_endpoints = []
            related_requests = []
            related_responses = []
            related_response_objects = []
            audio_candidates = []
            click_target = None
            def route_probe(route):
                request_url = route.request.url
                parsed_url = urlparse(request_url)
                # Stop at the site's own Download endpoint before it follows the redirect
                # to the MP3. Resolve that endpoint with a Range request after browser cookies
                # have been transferred; do not let Chromium fetch the full audio file.
                if (parsed_url.netloc.lower() in {"www.topdjmixes.com", "topdjmixes.com"}
                        and parsed_url.path.startswith("/download/")):
                    clicked_download_endpoints.append(request_url)
                    route.abort()
                elif is_mp3(request_url) or parsed_url.netloc.lower() == "files.topdjmixes.com":
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
                        if intercepted_audio or clicked_download_endpoints:
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
                clicked_download_endpoints=[
                    {"host": urlparse(u).netloc, "path": urlparse(u).path,
                     "query_present": bool(urlparse(u).query),
                     "url_if_no_query": u if not urlparse(u).query else None}
                    for u in clicked_download_endpoints[:10]
                ],
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

    # Resolve the site's own /download/<slug>/ endpoint using a one-byte Range request.
    # The browser route above aborts the endpoint before it can redirect to a full MP3 transfer.
    resolved_download_audio = []
    for endpoint in list(dict.fromkeys(clicked_download_endpoints))[:5]:
        response = None
        try:
            response = session.get(
                endpoint, headers={**HEADERS, "Referer": sample_url, "Range": "bytes=0-0"},
                allow_redirects=True, stream=True, timeout=(15, 30)
            )
            final_url = response.url
            parsed_final = urlparse(final_url)
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            content_range = response.headers.get("Content-Range", "")
            sample_bytes = 0
            if response.status_code == 206:
                sample_bytes = len(next(response.iter_content(1), b""))
            valid = (
                response.status_code in (200, 206)
                and (content_type.startswith("audio/") or is_mp3(final_url))
                and not parsed_final.path.lower().endswith((".html", ".htm"))
            )
            log("download_click_audio_range_probe",
                endpoint_path=urlparse(endpoint).path,
                final_host=parsed_final.netloc, final_path=parsed_final.path,
                final_query_present=bool(parsed_final.query),
                final_url_if_no_query=final_url if not parsed_final.query else None,
                status=response.status_code, content_type=content_type,
                content_range=content_range, range_requested="bytes=0-0",
                sample_bytes=sample_bytes, redirect_count=len(response.history), valid=valid)
            if valid and is_mp3(final_url):
                resolved_download_audio.append(final_url)
        except requests.RequestException as exc:
            log("download_click_endpoint_probe_error",
                endpoint_path=urlparse(endpoint).path, error=type(exc).__name__)
        finally:
            if response is not None:
                response.close()

    # Validate any MP3 URL exposed directly by the clicked control / small response bodies.
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
    description = extract_description(soup)
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


# Incremental archive implementation. The isolated test workflow and its script remain untouched.
import csv
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import timedelta
from email.utils import parsedate_to_datetime
from http.cookies import SimpleCookie
from urllib.parse import urlunparse, parse_qsl
from zoneinfo import ZoneInfo

REST_URL = BASE + "/wp-json/wp/v2/posts"
RETRY_CSV = Path("data/retry-queue.csv")
REPORT_CSV = Path("data/update-report.csv")
INVENTORY_CSV = Path("data/show-inventory.csv")
SUCCESS_REPORT_CSV = Path("data/success-report.csv")
REBUILD_ALL = os.environ.get("REBUILD_ALL", "").strip().lower() in ("1", "true", "yes", "on")
PER_PAGE = 100
MAX_REST_PAGES = int(os.environ.get("MAX_REST_PAGES", "600"))
EPISODES_PER_SHOW = 5
MAX_SERIES = int(os.environ.get("MAX_DISCOVERED_SHOWS", "300"))
REQUEST_DELAY = float(os.environ.get("REQUEST_DELAY_SECONDS", "0.5"))
REST_DELAY = float(os.environ.get("REST_REQUEST_DELAY_SECONDS", "0.1"))
RETRY_MAX_AGE_DAYS = int(os.environ.get("RETRY_MAX_AGE_DAYS", "45"))
RETRY_INITIAL_HOURS = 6
RETRY_MAX_HOURS = 72
ITUNES_NS = "http://www.itunes.com/dtds/podcast-1.0.dtd"
PODCAST_COVER_URL = "https://raw.githubusercontent.com/kuafor41/bestDJs/main/public/bestdjs-cover.jpg"
ET.register_namespace("itunes", ITUNES_NS)

def canonical_url(url: str) -> str:
    parsed = urlparse(html.unescape((url or "").strip()))
    if not parsed.scheme:
        parsed = urlparse(absolute(url))
    return urlunparse((parsed.scheme, parsed.netloc.lower(), parsed.path.rstrip("/") + "/", parsed.params, parsed.query, ""))

def safe_url(url: str):
    parsed = urlparse(url or "")
    result = {"host": parsed.netloc, "path": parsed.path, "query_present": bool(parsed.query)}
    if not parsed.query:
        result["url_if_no_query"] = url
    return result

def is_temporary_signed_url(url: str) -> bool:
    keys = [key.lower().replace("-", "_") for key, _ in parse_qsl(urlparse(url).query, keep_blank_values=True)]
    markers = ("expires", "expiry", "exp", "token", "signature", "sig", "policy",
               "key_pair_id", "x_amz_", "x_goog_", "hdnts", "auth", "st", "et")
    return any(key in ("e", "st", "et") or any(key == marker or key.startswith(marker) for marker in markers)
               for key in keys)

def validate_audio_url(session: requests.Session, url: str, referer: str = SHOWS_URL):
    """Range-check a single byte and reject HTML/login pages masquerading as an MP3."""
    response = None
    try:
        response = session.get(url, headers={**HEADERS, "Referer": referer, "Range": "bytes=0-0"},
                               allow_redirects=True, stream=True, timeout=(15, 30))
        final_url = response.url
        content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        content_range = response.headers.get("Content-Range", "")
        content_length = response.headers.get("Content-Length", "")
        length = None
        match = re.search(r"/(\d+|\*)$", content_range)
        if match and match.group(1) != "*":
            length = int(match.group(1))
        elif content_length.isdigit() and response.status_code == 200:
            length = int(content_length)
        html_response = content_type in ("text/html", "application/xhtml+xml") or "html" in content_type
        valid_type = content_type.startswith("audio/") or (
            is_mp3(final_url) and not html_response and not content_type.startswith("text/")
        )
        valid = response.status_code in (200, 206) and valid_type and is_mp3(final_url)
        return {"valid": valid, "url": final_url, "length": length, "status": response.status_code,
                "content_type": content_type, "content_range": content_range}
    except requests.RequestException as exc:
        return {"valid": False, "url": url, "length": None, "status": None, "error": type(exc).__name__}
    finally:
        if response is not None:
            response.close()

_original_extract_description = extract_description

def extract_description(soup: BeautifulSoup) -> str:
    """Match the working test-feed formatter, including Size-to-PARTS spacing."""
    entry = soup.select_one(".entry-content") or soup.select_one("article")
    if not entry:
        return ""
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
    for index, node in enumerate(nodes):
        if not getattr(node, "get_text", None):
            continue
        if re.match(r"^Date\s*:", visible_text(node), re.I):
            start_index = index
            break
    if start_index is None:
        for selector in ("p", "div", "section"):
            for node in entry.select(selector):
                text = visible_text(node)
                if re.match(r"^Date\s*:", text, re.I):
                    return text[:10000]
        # Retain the production extractor for layouts without a leading Date block.
        return _original_extract_description(soup) or clean_text(entry.get_text(" ", strip=True))[:10000]
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
    size_line = re.search(r"(?im)^Size\s*:[^\n]*", description)
    if size_line:
        tail = description[size_line.end():]
        if re.search(r"(?im)^\s*(?:Tracklist\s*:|PARTS\s*$|\d{1,3}[.)]\s+)", tail):
            description = description[:size_line.end()].rstrip() + "\n\n\n" + tail.lstrip("\r\n")
    return description[:10000] if description else ""

def parse_datetime_value(value):
    if not value:
        return None
    value = str(value).strip()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError):
        pass
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        pass
    for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y", "%Y-%m-%d",
                "%d-%m-%Y %H:%M", "%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%b %d %Y"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None

def page_date(text: str, soup: BeautifulSoup, rest_date: str):
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
        value = clean_text(node.get_text(" ", strip=True)) if node else ""
        if value:
            return re.sub(r"\s*[|–-]\s*Top\s*DJ\s*Mixes.*$", "", value, flags=re.I).strip() or fallback
    node = soup.select_one('meta[property="og:title"]')
    if node and node.get("content"):
        return clean_text(node["content"])
    return fallback

def extract_cover(soup: BeautifulSoup, page_url: str, episode_title: str):
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
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not re.search(r"\.(?:jpe?g|png|webp)(?:$|\?)", parsed.path, re.I):
            return
        path_label = normalize(parsed.path.rsplit("/", 1)[-1])
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
        if any(token in label for token in ("logo", "avatar", "favicon", "banner", "advert", "spinner", "placeholder")):
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
    return ranked[0][0] if ranked and ranked[0][1] >= 100 else None

def is_episode_page(soup: BeautifulSoup):
    control = soup.select_one("a.wpdm-download-link.download-on-click")
    if not control:
        return False
    body = clean_text((soup.select_one(".entry-content") or soup).get_text(" ", strip=True)).lower()
    return ("audio:" in body and "size:" in body) or "parts" in body

def transfer_cookies(context, session):
    for cookie in context.cookies():
        try:
            session.cookies.set(cookie["name"], cookie["value"], domain=cookie.get("domain"),
                                path=cookie.get("path", "/"), secure=cookie.get("secure", False))
        except Exception:
            continue

def login_in_browser(page, context, session, PlaywrightTimeoutError):
    page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=45000)
    try:
        page.wait_for_load_state("networkidle", timeout=20000)
    except PlaywrightTimeoutError:
        pass
    page.wait_for_timeout(1000)
    body = clean_text(page.locator("body").inner_text(timeout=10000)).lower()
    if "spam detected" in body:
        raise RuntimeError("TopDJMixes displayed Spam detected before login; no credentials were submitted.")
    user_field = page.locator('input[name="user_login"]:visible').last
    pass_field = page.locator('input[name="user_pass"]:visible').last
    for wait_step in range(1, 10):
        if user_field.count() and pass_field.count():
            break
        if wait_step < 9:
            page.wait_for_timeout(5000)
    if not user_field.count() or not pass_field.count():
        body = clean_text(page.locator("body").inner_text(timeout=10000))
        log("login_form_missing", title=page.title(), path=urlparse(page.url).path,
            body_excerpt=body[:300], credentials_submitted=False)
        raise RuntimeError("TopDJMixes did not present its normal login form; stopped before submitting credentials.")
    form = pass_field.locator("xpath=ancestor::form[1]")
    try:
        form.locator('input#arm_form_submission_key[name]:not([name=""])').wait_for(state="attached", timeout=15000)
    except PlaywrightTimeoutError as exc:
        log("login_form_not_initialized", title=page.title(), form_id=form.get_attribute("id") or "")
        raise RuntimeError("ARM Membership did not initialize its submission key; no login was submitted.") from exc
    honeypot = form.locator('input[name="arm_filter_input"]')
    if honeypot.count() and (honeypot.first.input_value() or "").strip():
        raise RuntimeError("ARM honeypot is not empty; refusing to submit login.")
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
    body = clean_text(page.locator("body").inner_text(timeout=10000))
    lowered = body.lower()
    auth_cookies = sorted({cookie["name"] for cookie in context.cookies()
                           if cookie["name"].startswith(("wordpress_logged_in_", "wordpress_sec_"))})
    logged_out_visible = page.get_by_text(re.compile(r"\blog ?out\b", re.I)).count() > 0
    login_form_visible = page.locator('input[name="user_login"]:visible').count() > 0
    log("browser_login_result", title=page.title(), path=urlparse(page.url).path,
        auth_cookie_names=auth_cookies, logged_out_visible=logged_out_visible,
        login_form_visible=login_form_visible, spam_detected="spam detected" in lowered,
        invalid_credentials_marker=any(marker in lowered for marker in (
            "incorrect password", "invalid username", "invalid password",
            "username or password is incorrect", "too many login attempts")))
    if "spam detected" in lowered:
        raise RuntimeError("TopDJMixes rejected the login submission as Spam detected; stopping.")
    if not auth_cookies or (login_form_visible and not logged_out_visible):
        raise RuntimeError("TopDJMixes did not confirm an authenticated session.")
    transfer_cookies(context, session)
    probe_url = BASE + "/the-midnight-project-228/"
    probe = session.get(probe_url, headers={"Referer": LOGIN_URL}, timeout=45)
    probe.raise_for_status()
    probe_soup = BeautifulSoup(probe.text, "html.parser")
    probe_body = clean_text((probe_soup.select_one(".entry-content") or probe_soup).get_text(" ", strip=True)).lower()
    gate = "please login to download or create a free account" in probe_body
    log("http_session_probe", status=probe.status_code, path=urlparse(probe.url).path,
        download_gate=gate, auth_cookie_count=len(auth_cookies))
    if gate:
        raise RuntimeError("Transferred HTTP session does not retain Download access.")

def extract_mixshow_catalog_links(soup: BeautifulSoup, page_url: str):
    """Extract only the root-level archive links from the site's Mix Shows directory."""
    excluded = {
        "mix-shows", "mixshow", "shows", "login", "register", "news", "music-player",
        "linklists", "schedule", "mobile-app", "contact", "privacy-policy", "terms",
        "terms-conditions", "gdpr", "my-account", "membership-account", "wp-admin",
        "wp-content", "wp-json", "category", "tag", "page", "author", "search",
        "feed", "lost-password", "upgrade", "download",
    }

    def links_in(container):
        found = {}
        for anchor in container.select("a[href]"):
            href = absolute(anchor.get("href", ""), page_url)
            parsed = urlparse(href)
            if parsed.netloc.lower() not in ("www.topdjmixes.com", "topdjmixes.com"):
                continue
            parts = [part for part in parsed.path.split("/") if part]
            if len(parts) != 1:
                continue
            slug = parts[0].lower()
            if slug in excluded or slug.startswith(("wp-", "page-")) or re.search(r"-\d+$", slug):
                continue
            title = clean_text(anchor.get_text(" ", strip=True)) or clean_text(anchor.get("title", ""))
            if not title or len(title) < 2 or title.lower() in ("mix shows", "next", "previous"):
                continue
            found[canonical_url(href)] = title
        return found

    best = {}
    for heading in soup.find_all(re.compile(r"^h[1-6]$")):
        if clean_text(heading.get_text(" ", strip=True)).lower() != "mix shows":
            continue
        ancestor = heading
        for _ in range(8):
            ancestor = ancestor.parent
            if not getattr(ancestor, "select", None):
                break
            candidates = links_in(ancestor)
            if len(candidates) >= 50:
                if len(candidates) > len(best):
                    best = candidates
                break
    if len(best) < 50:
        fallback = links_in(soup)
        if len(fallback) > len(best):
            best = fallback
    return best


def discover_episode_posts(session):
    """Discover show archives from the site menu and fetch recent links inside each one."""
    seeds = [SHOWS_URL, BASE + "/intelligent-waveforms/", BASE + "/a-dream-radio/"]
    mixshows = {}
    seed_results = []
    for seed in seeds:
        try:
            response = session.get(seed, headers={"Referer": SHOWS_URL}, timeout=45)
            response.raise_for_status()
            found = extract_mixshow_catalog_links(BeautifulSoup(response.text, "html.parser"), response.url)
            seed_results.append({"path": urlparse(seed).path, "count": len(found)})
            log("mixshow_catalog_probe", seed_path=urlparse(seed).path,
                found=len(found), sample=list(found.items())[:4])
            if len(found) > len(mixshows):
                mixshows = found
            if len(mixshows) >= 100:
                break
        except requests.RequestException as exc:
            log("mixshow_catalog_probe_error", seed_path=urlparse(seed).path, error=type(exc).__name__)

    if len(mixshows) < 50:
        raise RuntimeError(
            "Mix Shows directory discovery returned fewer than 50 archives; refusing to alter production RSS. "
            f"Probe results: {seed_results}"
        )
    if len(mixshows) > MAX_SERIES:
        raise RuntimeError(f"Found {len(mixshows)} mixshows, above safety limit {MAX_SERIES}.")

    log("mixshow_catalog_discovered", count=len(mixshows),
        sample=[{"url": safe_url(url), "title": title} for url, title in list(mixshows.items())[:12]])
    max_candidates = int(os.environ.get("MAX_CANDIDATES_PER_SHOW", "20"))
    max_pages = int(os.environ.get("MAX_ARCHIVE_PAGES_PER_SHOW", "3"))
    posts, globally_seen, census = [], set(), []

    # This is intentionally bounded to each mixshow's own archive page(s), not
    # the 41k-post site-wide WordPress archive.
    for index, (show_url, menu_title) in enumerate(sorted(mixshows.items()), 1):
        show_slug = urlparse(show_url).path.strip("/").split("/")[-1].lower()
        show_title = menu_title
        page_url = show_url
        page_number = 1
        pages_read = 0
        candidates = []
        show_seen = set()
        archive_error = ""

        while page_url and pages_read < max_pages and len(candidates) < max_candidates:
            try:
                response = session.get(page_url, headers={"Referer": SHOWS_URL}, timeout=45)
                response.raise_for_status()
            except requests.RequestException as exc:
                archive_error = type(exc).__name__
                log("mixshow_archive_fetch_error", show=show_slug, page=safe_url(page_url), error=archive_error)
                break
            soup = BeautifulSoup(response.text, "html.parser")
            pages_read += 1
            h1 = soup.select_one("h1.entry-title") or soup.select_one("article h1") or soup.select_one("h1")
            if h1:
                parsed_title = clean_text(h1.get_text(" ", strip=True))
                if parsed_title:
                    show_title = re.sub(r"\s*[|–-]\s*Top\s*DJ\s*Mixes.*$", "", parsed_title, flags=re.I).strip() or show_title

            for episode_url, episode_title in extract_show_links(soup, response.url).items():
                episode_url = canonical_url(episode_url)
                if episode_url in show_seen or episode_url in globally_seen:
                    continue
                show_seen.add(episode_url)
                globally_seen.add(episode_url)
                slug = urlparse(episode_url).path.strip("/").split("/")[-1]
                candidates.append({
                    "link": episode_url, "slug": slug, "title": {"rendered": episode_title},
                    "series_key": show_slug, "series_title": show_title,
                    "date": "", "date_gmt": "",
                })
                if len(candidates) >= max_candidates:
                    break

            show_path = urlparse(show_url).path.rstrip("/")
            next_pages = []
            for anchor in soup.select("a[href]"):
                href = absolute(anchor.get("href", ""), response.url)
                parsed = urlparse(href)
                if parsed.netloc.lower() not in ("www.topdjmixes.com", "topdjmixes.com"):
                    continue
                match = re.fullmatch(re.escape(show_path) + r"/page/(\d+)/?", parsed.path)
                if match and int(match.group(1)) > page_number:
                    next_pages.append((int(match.group(1)), href))
            if len(candidates) >= max_candidates or not next_pages:
                break
            page_number, page_url = min(next_pages, key=lambda item: item[0])
            if REST_DELAY:
                time.sleep(REST_DELAY)

        census.append({
            "show_slug": show_slug, "show_title": show_title,
            "source_episode_candidates": len(candidates),
            "archive_pages_read": pages_read, "archive_error": archive_error,
        })
        posts.extend(candidates)
        if index == 1 or index % 10 == 0 or index == len(mixshows):
            log("mixshow_discovery_progress", checked=index, total_mixshows=len(mixshows),
                episode_candidates=len(posts))
        if REQUEST_DELAY:
            time.sleep(REQUEST_DELAY)

    log("mixshow_episode_discovery_complete", mixshows=len(mixshows), candidates=len(posts),
        candidates_per_show=max_candidates, archive_pages_per_show=max_pages,
        shows_with_no_episode_links=sum(1 for row in census if row["source_episode_candidates"] == 0),
        shows_with_archive_errors=sum(1 for row in census if row["archive_error"]))
    if not posts:
        raise RuntimeError("Mixshow archives were found, but no episode links were discovered.")
    return posts, census


def series_key_for_post(post):
    explicit = str(post.get("series_key") or "").strip().lower()
    if explicit:
        return explicit
    slug = str(post.get("slug") or urlparse(post.get("link", "")).path.strip("/").split("/")[-1]).lower().strip("/")
    match = re.fullmatch(r"(?P<series>[a-z0-9][a-z0-9-]*?)-(?P<episode>\d+[a-z]?)", slug, flags=re.I)
    if not match:
        return None
    key = match.group("series").strip("-")
    if len(key) < 2 or key in {"episode", "mix", "mixshow", "show", "news", "review", "top"}:
        return None
    return key


def group_posts_by_series(posts):
    groups = defaultdict(list)
    for post in posts:
        key = post.get("series_key") or series_key_for_post(post)
        if key:
            groups[key].append(post)
    # Do not re-sort by episode number: source archive-page order is newest-first.
    log("series_grouping_complete", candidate_series=len(groups),
        total_episode_candidates=sum(len(rows) for rows in groups.values()),
        series_sample=[{"slug": key, "candidate_pages": len(rows),
                        "latest_title": rest_title(rows[0])} for key, rows in list(groups.items())[:12]])
    return groups

def rest_date(post):
    return parse_datetime_value(post.get("date_gmt") or post.get("date")) or datetime.min.replace(tzinfo=timezone.utc)

def rest_title(post):
    title = post.get("title", "")
    if isinstance(title, dict):
        title = title.get("rendered", "")
    return clean_text(BeautifulSoup(html.unescape(str(title)), "html.parser").get_text(" ", strip=True))

def group_posts_by_series(posts):
    groups = defaultdict(list)
    for post in posts:
        key = series_key_for_post(post)
        if key:
            groups[key].append(post)
    for key in groups:
        groups[key].sort(key=rest_date, reverse=True)
    if len(groups) > MAX_SERIES:
        raise RuntimeError(f"Found {len(groups)} numbered-slug groups, above safety limit {MAX_SERIES}; refusing a misclassified archive.")
    log("series_grouping_complete", candidate_series=len(groups),
        series_sample=[{"slug": key, "latest_title": rest_title(rows[0])} for key, rows in list(groups.items())[:12]])
    return groups

def inspect_episode_post(session, post, series_key):
    url = post["link"]
    fallback_title = rest_title(post) or urlparse(url).path.strip("/").split("/")[-1]
    try:
        response = session.get(url, headers={"Referer": SHOWS_URL}, timeout=35)
    except requests.RequestException as exc:
        return None, f"episode_page_request_{type(exc).__name__}", True
    if response.status_code >= 400:
        return None, f"episode_page_http_{response.status_code}", True
    soup = BeautifulSoup(response.text, "html.parser")
    if not is_episode_page(soup):
        return None, "not_an_episode_page", False
    title = extract_title(soup, fallback_title)
    entry = soup.select_one(".entry-content") or soup
    visible_text = clean_text(entry.get_text(" ", strip=True))
    date, date_source = page_date(visible_text, soup, post.get("date_gmt") or post.get("date") or "")
    if not date:
        return None, "episode_date_missing", True
    description = extract_description(soup) or title
    cover = extract_cover(soup, response.url, title)
    player_audio_candidate = find_audio(soup, response.url)
    page_url = canonical_url(response.url)
    item = {
        "title": title, "page": page_url, "guid": page_url, "slug": post.get("slug", ""),
        "series_key": series_key,
        "series_title": re.sub(r"\s+(?:episode\s*)?#?\d+[a-z]?$", "", title, flags=re.I).strip() or series_key.replace("-", " ").title(),
        "date": date, "date_source": date_source, "description": description, "cover": cover,
        "player_audio_candidate": player_audio_candidate,
        "audio": "", "audio_length": 0,
    }
    log("episode_page_parsed", show=series_key, page=safe_url(page_url), title=title,
        date=date.isoformat(), date_source=date_source, description_chars=len(description),
        cover_found=bool(cover),
        tracklist_found=bool(re.search(r"(?im)^\s*(?:PARTS|Tracklist:|\d{1,3}[.)]\s+)", description)))
    return item, None, True

def click_download_and_resolve_audio(page, context, session, item, PlaywrightTimeoutError):
    """Check both the embedded player source and the Download control without downloading MP3 bytes."""
    player_result = None
    player_candidate = item.get("player_audio_candidate")
    if player_candidate and is_mp3(player_candidate):
        player_check = validate_audio_url(session, player_candidate, referer=item.get("page", SHOWS_URL))
        player_final = player_check.get("url", player_candidate)
        player_temporary = is_temporary_signed_url(player_final)
        player_valid = player_check.get("valid") and is_mp3(player_final) and not player_temporary
        log("episode_player_audio_probe", title=item.get("title", ""), page=safe_url(item.get("page", "")),
            candidate_url=safe_url(player_candidate), valid=player_valid,
            status=player_check.get("status"), content_type=player_check.get("content_type"),
            final_host=urlparse(player_final).netloc, temporary_signature=player_temporary)
        if player_valid:
            player_result = (player_final, player_check.get("length"))

    def use_player_fallback(reason):
        if player_result:
            log("episode_audio_player_fallback", title=item.get("title", ""),
                page=safe_url(item.get("page", "")), reason=reason,
                audio_host=urlparse(player_result[0]).netloc)
            return player_result
        return None, None
    try:
        page.goto(item["page"], wait_until="domcontentloaded", timeout=45000)
    except Exception as exc:
        log("episode_browser_navigation_error", title=item["title"], page=safe_url(item["page"]), error=type(exc).__name__)
        return use_player_fallback("download_page_navigation_failed")
    try:
        page.locator("a.wpdm-download-link.download-on-click:visible").wait_for(state="attached", timeout=12000)
    except PlaywrightTimeoutError:
        pass
    page.wait_for_timeout(350)
    body = clean_text(page.locator("body").inner_text(timeout=10000)).lower()
    if "please login to download or create a free account" in body:
        log("episode_browser_login_gate", title=item["title"], page=safe_url(item["page"]))
        return use_player_fallback("download_login_gate")
    button = page.locator("a.wpdm-download-link.download-on-click:visible").first
    if not button.count():
        log("episode_download_button_missing", title=item["title"], page=safe_url(item["page"]))
        return use_player_fallback("download_button_missing")
    captured = []
    redirect_trace = []
    def route_download(route):
        request = route.request
        parsed = urlparse(request.url)
        if parsed.netloc.lower() in ("www.topdjmixes.com", "topdjmixes.com") and parsed.path.startswith("/download/"):
            current_url = request.url
            previous_url = request.url
            headers = dict(request.headers)
            try:
                browser_cookies = context.cookies("https://www.topdjmixes.com")
                cookie_values = {cookie["name"]: cookie["value"] for cookie in browser_cookies}
                existing = headers.get("cookie", headers.get("Cookie", ""))
                for pair in existing.split(";"):
                    if "=" in pair:
                        name, value = pair.split("=", 1)
                        cookie_values.setdefault(name.strip(), value.strip())
                if cookie_values:
                    headers["cookie"] = "; ".join(f"{key}={value}" for key, value in cookie_values.items())
            except Exception:
                pass
            for hop in range(1, 7):
                if is_mp3(current_url):
                    captured.append(current_url)
                    break
                response = None
                try:
                    hop_headers = dict(headers)
                    if previous_url:
                        hop_headers["referer"] = previous_url
                    response = route.fetch(url=current_url, headers=hop_headers, max_redirects=0, timeout=15000)
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
                            for pair in hop_headers.get("cookie", "").split(";"):
                                if "=" in pair:
                                    name, value = pair.split("=", 1)
                                    values[name.strip()] = value.strip()
                            for name, morsel in jar.items():
                                values[name] = morsel.value
                            headers["cookie"] = "; ".join(f"{name}={value}" for name, value in values.items())
                        except Exception:
                            pass
                    if status in (301, 302, 303, 307, 308) and location:
                        next_url = urljoin(response.url, location)
                        if is_mp3(next_url):
                            captured.append(next_url)
                            break
                        previous_url, current_url = response.url, next_url
                        continue
                    break
                except Exception as exc:
                    redirect_trace.append({"hop": hop, "error": type(exc).__name__, "url": safe_url(current_url)})
                    break
                finally:
                    if response is not None:
                        try:
                            response.dispose()
                        except Exception:
                            pass
            route.abort()
            return
        if is_mp3(request.url) or parsed.netloc.lower() == "files.topdjmixes.com":
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
        click_error = type(exc).__name__
    finally:
        try:
            context.unroute("**/*", route_download)
        except Exception:
            pass
    unique_urls = list(dict.fromkeys(captured))
    if not unique_urls:
        log("episode_download_click_no_mp3", title=item["title"], page=safe_url(item["page"]),
            click_error=click_error, redirect_trace=redirect_trace[:8])
        return use_player_fallback("download_button_no_mp3_url")
    audio_url = unique_urls[0]
    checked = validate_audio_url(session, audio_url, referer=item["page"])
    final_url = checked.get("url", audio_url)
    final = urlparse(final_url)
    temporary = is_temporary_signed_url(final_url)
    log("episode_audio_range_check", title=item["title"], page=safe_url(item["page"]),
        audio_url=safe_url(audio_url), valid=checked.get("valid"), status=checked.get("status"),
        content_type=checked.get("content_type"), content_range=checked.get("content_range"),
        length=checked.get("length"), final_host=final.netloc, final_path=final.path,
        final_query_present=bool(final.query), temporary_signature=temporary,
        click_error=click_error, redirect_trace=redirect_trace[:8])
    if not checked.get("valid") or not is_mp3(final_url) or temporary:
        if temporary:
            log("audio_url_rejected_temporary_signature", title=item["title"], final_url=safe_url(final_url))
        return use_player_fallback("download_audio_invalid_or_temporary")
    return final_url, checked.get("length")

def read_existing_feed():
    if not OUT.exists() or OUT.stat().st_size == 0:
        return []
    root = ET.parse(OUT).getroot()
    items = []
    for node in root.findall("./channel/item"):
        title = node.findtext("title") or ""
        page = node.findtext("link") or node.findtext("guid") or ""
        guid = node.findtext("guid") or page
        pub_date = parse_datetime_value(node.findtext("pubDate")) or datetime.min.replace(tzinfo=timezone.utc)
        enclosure = node.find("enclosure")
        audio = enclosure.get("url", "") if enclosure is not None else ""
        if not title or not page or not audio:
            continue
        image_node = node.find(f"{{{ITUNES_NS}}}image")
        items.append({"title": title, "page": canonical_url(page), "guid": canonical_url(guid),
                      "date": pub_date, "description": node.findtext("description") or title,
                      "cover": image_node.get("href", "") if image_node is not None else "",
                      "audio": audio, "audio_length": int(enclosure.get("length", "0") or 0)})
    return items

RETRY_FIELDS = ["episode_url", "show_slug", "show_title", "episode_title", "source_date",
                "first_seen_utc", "last_attempt_utc", "attempts", "next_retry_utc", "last_error", "status"]

def read_retry_queue():
    if not RETRY_CSV.exists():
        return {}
    rows = {}
    with RETRY_CSV.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            url = canonical_url(row.get("episode_url", ""))
            if url:
                row["episode_url"] = url
                rows[url] = row
    return rows

def write_retry_queue(queue):
    RETRY_CSV.parent.mkdir(parents=True, exist_ok=True)
    with RETRY_CSV.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=RETRY_FIELDS)
        writer.writeheader()
        for row in sorted(queue.values(), key=lambda item: (item.get("first_seen_utc", ""), item.get("episode_url", ""))):
            writer.writerow({field: row.get(field, "") for field in RETRY_FIELDS})

def utc_iso(value):
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat()

def add_retry(queue, url, series_key, series_title, episode_title, source_date, error, now):
    url = canonical_url(url)
    if url in queue:
        return
    queue[url] = {
        "episode_url": url, "show_slug": series_key or "", "show_title": series_title or series_key or "",
        "episode_title": episode_title or urlparse(url).path.strip("/"),
        "source_date": utc_iso(source_date) if isinstance(source_date, datetime) else str(source_date or ""),
        "first_seen_utc": utc_iso(now), "last_attempt_utc": utc_iso(now), "attempts": "1",
        "next_retry_utc": utc_iso(now + timedelta(hours=RETRY_INITIAL_HOURS)),
        "last_error": error, "status": "waiting",
    }
    log("retry_queue_added", page=safe_url(url), show=series_key, error=error)

def update_retry_failure(row, error, now):
    first_seen = parse_datetime_value(row.get("first_seen_utc")) or now
    attempts = int(row.get("attempts") or 0) + 1
    row["attempts"] = str(attempts)
    row["last_attempt_utc"] = utc_iso(now)
    row["last_error"] = error
    if now - first_seen >= timedelta(days=RETRY_MAX_AGE_DAYS):
        row["status"] = "expired"
        row["next_retry_utc"] = ""
    else:
        hours = min(RETRY_MAX_HOURS, RETRY_INITIAL_HOURS * (2 ** min(attempts - 1, 10)))
        row["next_retry_utc"] = utc_iso(now + timedelta(hours=hours))
        row["status"] = "waiting"
    log("retry_queue_attempt_failed", page=safe_url(row.get("episode_url", "")),
        attempts=attempts, status=row["status"], error=error, next_retry=row.get("next_retry_utc", ""))

def retry_is_due(row, now):
    if row.get("status", "waiting") != "waiting":
        return False
    first_seen = parse_datetime_value(row.get("first_seen_utc")) or now
    if now - first_seen >= timedelta(days=RETRY_MAX_AGE_DAYS):
        row["status"] = "expired"
        row["next_retry_utc"] = ""
        return False
    next_retry = parse_datetime_value(row.get("next_retry_utc"))
    return next_retry is None or next_retry <= now

def write_rss(items):
    OUT.parent.mkdir(parents=True, exist_ok=True)
    items.sort(key=lambda item: item["date"], reverse=True)
    now = datetime.now(timezone.utc)
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">',
        '<channel>', '<title>bestDJs Podcast</title>',
        f'<link>{xml_escape(SHOWS_URL)}</link>',
        '<description>bestDJs Podcast — authenticated TopDJMixes mixshows with directly verified MP3 enclosures.</description>',
        '<language>en-us</language>', '<itunes:author>bestDJs</itunes:author>',
        '<itunes:category text="Music"/>', '<itunes:explicit>no</itunes:explicit>',
        f'<lastBuildDate>{format_datetime(now, usegmt=True)}</lastBuildDate>',
    ]
    lines.append(f'<itunes:image href="{xml_escape(PODCAST_COVER_URL)}"/>')
    for item in items:
        date = item.get("date") or datetime.min.replace(tzinfo=timezone.utc)
        title = item.get("title") or item.get("guid") or item.get("page")
        description = item.get("description") or title
        lines.extend([
            '<item>', f'<title>{xml_escape(title)}</title>',
            f'<link>{xml_escape(item.get("page") or item.get("guid"))}</link>',
            f'<guid isPermaLink="true">{xml_escape(item.get("guid") or item.get("page"))}</guid>',
            f'<pubDate>{format_datetime(date.astimezone(timezone.utc), usegmt=True)}</pubDate>',
            f'<description>{xml_escape(description)}</description>',
        ])
        if item.get("cover"):
            lines.append(f'<itunes:image href="{xml_escape(item["cover"])}"/>')
        lines.extend([
            f'<enclosure url="{xml_escape(item["audio"])}" length="{int(item.get("audio_length") or 0)}" type="audio/mpeg"/>',
            '<itunes:explicit>no</itunes:explicit>', '</item>',
        ])
    lines.extend(["</channel>", "</rss>", ""])
    OUT.write_text("\n".join(lines), encoding="utf-8")

def validate_guid_retention(previous_items, current_items):
    """Fail closed if any GUID from the previously published feed disappears."""
    previous_guids = {
        canonical_url(item.get("guid") or item.get("page", ""))
        for item in previous_items
        if item.get("guid") or item.get("page")
    }
    current_guids = {
        canonical_url(item.get("guid") or item.get("page", ""))
        for item in current_items
        if item.get("guid") or item.get("page")
    }
    missing = sorted(guid for guid in previous_guids if guid not in current_guids)
    if missing:
        preview = ", ".join(missing[:10])
        more = f" (and {len(missing) - 10} more)" if len(missing) > 10 else ""
        raise RuntimeError(
            f"RSS history protection failed: {len(missing)} previously published GUID(s) "
            f"would be removed: {preview}{more}. Production RSS was not written."
        )
    log("rss_guid_history_protection_passed",
        previous_guid_count=len(previous_guids), current_guid_count=len(current_guids),
        retained_count=len(previous_guids))


def validate_feed(items):
    root = ET.parse(OUT).getroot()
    channel_image = root.find(f"./channel/{{{ITUNES_NS}}}image")
    if channel_image is None or channel_image.get("href") != PODCAST_COVER_URL:
        raise RuntimeError("RSS channel artwork is missing or does not match the committed bestDJs cover.")
    entries = root.findall("./channel/item")
    if not entries:
        raise RuntimeError("Production RSS has no valid episodes; refusing an empty published feed.")
    dates, guids = [], set()
    for entry in entries:
        title = entry.findtext("title") or ""
        if not title or not entry.findtext("description") or not entry.findtext("pubDate") or not entry.findtext("guid"):
            raise RuntimeError(f"RSS item is missing title, description, date, or GUID: {title}")
        image_node = entry.find(f"{{{ITUNES_NS}}}image")
        if image_node is None or not image_node.get("href"):
            raise RuntimeError(f"RSS item is missing artwork: {title}")
        guid = entry.findtext("guid")
        if guid in guids:
            raise RuntimeError(f"Duplicate RSS GUID detected: {guid}")
        guids.add(guid)
        enclosure = entry.find("enclosure")
        if enclosure is None or enclosure.get("type") != "audio/mpeg":
            raise RuntimeError(f"RSS item missing audio/mpeg enclosure: {title}")
        url = enclosure.get("url", "")
        if not is_mp3(url) or is_temporary_signed_url(url):
            raise RuntimeError(f"RSS item does not have a stable direct MP3 URL: {title}")
        parsed = parse_datetime_value(entry.findtext("pubDate"))
        if not parsed:
            raise RuntimeError(f"RSS item has an invalid publication date: {title}")
        dates.append(parsed)
    if dates != sorted(dates, reverse=True):
        raise RuntimeError("RSS items are not sorted newest to oldest.")
    log("rss_validation_passed", path=str(OUT), items=len(entries),
        newest_date=dates[0].isoformat(), oldest_date=dates[-1].isoformat(),
        unique_guids=len(guids), sorted_newest_to_oldest=True)

def apply_series_metadata(items, catalog_rows, posts):
    """Attach show identity to every retained RSS item using the catalogue and episode URLs."""
    known = {}
    for row in catalog_rows:
        slug = str(row.get("show_slug") or "").strip().lower()
        title = str(row.get("show_title") or slug.replace("-", " ").title()).strip()
        if slug:
            known[slug] = title
    if INVENTORY_CSV.exists():
        try:
            with INVENTORY_CSV.open("r", encoding="utf-8-sig", newline="") as handle:
                for old in csv.DictReader(handle):
                    slug = str(old.get("show_slug") or "").strip().lower()
                    if slug:
                        known.setdefault(slug, str(old.get("show_title") or slug.replace("-", " ").title()))
        except (OSError, csv.Error):
            pass
    ordered = sorted(known.items(), key=lambda pair: len(pair[0]), reverse=True)
    by_page = {canonical_url(post.get("link", "")): post for post in posts if post.get("link")}
    def normal(value):
        return re.sub(r"[^a-z0-9]+", " ", unquote(str(value or "")).lower()).strip()
    for item in items:
        key = str(item.get("series_key") or "").strip().lower()
        show_title = str(item.get("series_title") or "").strip()
        post = by_page.get(canonical_url(item.get("page") or item.get("guid") or ""))
        if not key and post:
            key = str(post.get("series_key") or series_key_for_post(post) or "").strip().lower()
            show_title = str(post.get("series_title") or "")
        page_slug = urlparse(item.get("page") or "").path.strip("/").split("/")[-1].lower()
        if not key:
            for candidate_slug, candidate_title in ordered:
                if page_slug == candidate_slug or page_slug.startswith(candidate_slug + "-"):
                    key, show_title = candidate_slug, candidate_title
                    break
        if not key:
            title_norm = normal(item.get("title"))
            for candidate_slug, candidate_title in ordered:
                candidate_norm = normal(candidate_title)
                if candidate_norm and title_norm.startswith(candidate_norm):
                    key, show_title = candidate_slug, candidate_title
                    break
        if key:
            item["series_key"] = key
            if show_title:
                item["series_title"] = show_title
            elif key in known:
                item["series_title"] = known[key]



def canonicalize_series_covers(items):
    # Normalize only clearly equivalent cover variants: same basename, optional
    # WordPress duplicate suffix "-2", different file formats. Truly episode-specific
    # image filenames (for example, numbered episode artwork) are left untouched.
    groups = defaultdict(list)
    for item in items:
        if item.get("series_key"):
            groups[item["series_key"]].append(item)
    def cover_key(url):
        stem = Path(unquote(urlparse(url).path)).stem.lower()
        stem = re.sub(r"-2$", "", stem)
        return re.sub(r"[^a-z0-9]+", "-", stem).strip("-")
    def cover_rank(url):
        path = urlparse(url).path.lower()
        ext = Path(path).suffix
        # Prefer formats widely accepted by podcast apps over WebP when equivalent.
        return ({".jpg": 4, ".jpeg": 4, ".png": 3, ".webp": 1}.get(ext, 0),
                len(path))
    for slug, rows in groups.items():
        by_stem = defaultdict(list)
        for item in rows:
            cover = item.get("cover")
            if cover:
                by_stem[cover_key(cover)].append(cover)
        if len(by_stem) == 1:
            only_stem = next(iter(by_stem))
            candidates = sorted(set(by_stem[only_stem]), key=cover_rank, reverse=True)
            canonical = candidates[0]
            for item in rows:
                if item.get("cover") or not by_stem:
                    if item.get("cover") and cover_key(item["cover"]) == only_stem:
                        item["cover"] = canonical
                    elif not item.get("cover"):
                        item["cover"] = canonical
            if len(set(by_stem[only_stem])) > 1:
                log("series_cover_variants_normalized", show=slug, variants=len(set(by_stem[only_stem])),
                    chosen_extension=Path(urlparse(canonical).path).suffix.lower())

    # Items with no usable show art still get a valid image in podcast apps.
    for item in items:
        if not item.get("cover"):
            item["cover"] = PODCAST_COVER_URL

def write_inventory(show_rows, now, all_items=None, discovered_posts=None):
    """Write a cumulative inventory; preserve older shows and episode links across every run."""
    INVENTORY_CSV.parent.mkdir(parents=True, exist_ok=True)
    all_items = all_items or []
    discovered_posts = discovered_posts or []
    by_title = {item.get("title", ""): item for item in all_items if item.get("title")}
    previous = {}
    if INVENTORY_CSV.exists():
        try:
            with INVENTORY_CSV.open("r", encoding="utf-8-sig", newline="") as handle:
                for old in csv.DictReader(handle):
                    slug = str(old.get("show_slug") or "").strip().lower()
                    if not slug:
                        continue
                    entries = []
                    for line in (old.get("episodes_for_radioshow") or "").splitlines():
                        match = re.match(r"^\s*-\s*\[(.*?)\]\((https?://[^)]+)\)\s*$", line)
                        if match:
                            title, url = match.group(1).strip(), canonical_url(match.group(2).strip())
                            entries.append({"title": title, "url": url})
                    if not entries:
                        for title in (old.get("selected_titles") or "").split("; "):
                            item = by_title.get(title)
                            if title and item and item.get("page"):
                                entries.append({"title": title, "url": canonical_url(item["page"])})
                    previous[slug] = {"title": old.get("show_title") or slug.replace("-", " ").title(),
                                      "entries": entries, "row": dict(old)}
        except (OSError, csv.Error):
            # Do not silently erase a corrupted prior inventory. A malformed existing
            # file should stop the run so its historical entries can be recovered.
            raise RuntimeError("Previous show-inventory.csv could not be read safely; refusing to overwrite history.")

    current_rows = {str(row.get("show_slug") or "").strip().lower(): dict(row)
                    for row in show_rows if row.get("show_slug")}
    merged = {}
    for slug, value in previous.items():
        merged[slug] = {"title": value["title"], "entries": list(value["entries"])}
    for slug, row in current_rows.items():
        merged.setdefault(slug, {"title": row.get("show_title") or slug.replace("-", " ").title(), "entries": []})
        merged[slug]["title"] = row.get("show_title") or merged[slug]["title"]

    # Every retained feed item is added to its owning show. This includes old episodes
    # that no longer appear among the latest archive-page candidates.
    for item in all_items:
        slug = str(item.get("series_key") or "").strip().lower()
        title, url = item.get("title", ""), item.get("page", "")
        if not slug or not title or not url:
            continue
        merged.setdefault(slug, {"title": item.get("series_title") or slug.replace("-", " ").title(), "entries": []})
        entries = merged[slug]["entries"]
        url = canonical_url(url)
        if not any(canonical_url(entry.get("url", "")) == url for entry in entries):
            entries.append({"title": title, "url": url})

    # Add every episode link seen in the current show archive pages, not just the
    # five currently selected into RSS. The inventory grows cumulatively over runs.
    discovered_order = {}
    for index, post in enumerate(discovered_posts):
        url = canonical_url(post.get("link") or "")
        slug = str(post.get("series_key") or series_key_for_post(post) or "").strip().lower()
        title = rest_title(post) or post.get("slug") or url
        if not url or not slug or not title:
            continue
        discovered_order.setdefault(url, index)
        merged.setdefault(slug, {
            "title": post.get("series_title") or slug.replace("-", " ").title(),
            "entries": [],
        })
        entries = merged[slug]["entries"]
        if not any(canonical_url(entry.get("url", "")) == url for entry in entries):
            entries.append({"title": title, "url": url})

    # Migrate selected titles from the former short inventory format where possible.
    for slug, row in current_rows.items():
        entries = merged[slug]["entries"]
        for title in (row.get("selected_titles") or "").split("; "):
            item = by_title.get(title)
            if title and item and item.get("page"):
                url = canonical_url(item["page"])
                if not any(canonical_url(entry.get("url", "")) == url for entry in entries):
                    entries.append({"title": title, "url": url})

    item_by_url = {canonical_url(item.get("page") or ""): item for item in all_items if item.get("page")}
    fields = ["run_at_utc", "show_slug", "show_title", "source_episode_candidates",
              "archive_status", "episode_pages_selected", "added_this_run", "already_in_feed",
              "queued_failures", "latest_selected_title", "latest_selected_date", "selected_titles",
              "episodes_for_radioshow"]
    with INVENTORY_CSV.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for slug in sorted(set(previous) | set(current_rows) | set(merged)):
            row = current_rows.get(slug) or previous.get(slug, {}).get("row", {})
            title = (current_rows.get(slug, {}).get("show_title")
                     or merged.get(slug, {}).get("title") or row.get("show_title")
                     or slug.replace("-", " ").title())
            entries_by_url = {}
            for entry in merged.get(slug, {}).get("entries", []):
                url = canonical_url(entry.get("url", ""))
                if url and entry.get("title"):
                    entries_by_url[url] = {"title": entry["title"], "url": url}
            entries = list(entries_by_url.values())
            def entry_sort_key(entry):
                url = canonical_url(entry["url"])
                rss_item = item_by_url.get(url)
                if rss_item and rss_item.get("date"):
                    return (2, rss_item["date"].timestamp(), entry["title"])
                if url in discovered_order:
                    # Discovery follows each show archive's newest-first source order.
                    return (1, -discovered_order[url], entry["title"])
                return (0, 0.0, entry["title"])
            entries.sort(key=entry_sort_key, reverse=True)
            formatted = "\n".join(
                f"- [{entry['title'].replace(']', '')}]({entry['url']})"
                for entry in entries if entry.get("title") and entry.get("url")
            )
            output = {field: row.get(field, "") for field in fields}
            output["run_at_utc"] = utc_iso(now) if slug in current_rows else (row.get("run_at_utc") or "")
            output["show_slug"] = slug
            output["show_title"] = title
            output["episodes_for_radioshow"] = formatted
            if slug in current_rows and not output.get("archive_status"):
                output["archive_status"] = "ok" if int(row.get("source_episode_candidates") or 0) else "no_episode_links"
            writer.writerow(output)



def append_update_report(now, rebuild, source_posts, show_count, selected_count,
                         added_items, retry_attempted, recovered_count, queue, failures):
    REPORT_CSV.parent.mkdir(parents=True, exist_ok=True)
    fields = ["date_tr", "time_tr", "run_type", "mixshows_discovered", "source_posts_scanned",
              "episodes_selected", "added_count", "retry_attempted", "recovered_count",
              "failed_links", "retry_waiting", "retry_expired", "result", "episode_titles"]
    local = now.astimezone(ZoneInfo("Europe/Istanbul"))
    waiting = sum(1 for row in queue.values() if row.get("status") == "waiting")
    expired = sum(1 for row in queue.values() if row.get("status") == "expired")
    titles = "; ".join(item["title"] for item in added_items) if added_items else "No new episodes"
    row = {"date_tr": local.strftime("%d.%m.%Y"), "time_tr": local.strftime("%H:%M:%S"),
        "run_type": "full_rebuild" if rebuild else "incremental", "mixshows_discovered": show_count,
        "source_posts_scanned": source_posts, "episodes_selected": selected_count,
        "added_count": len(added_items), "retry_attempted": retry_attempted,
        "recovered_count": recovered_count, "failed_links": failures, "retry_waiting": waiting,
        "retry_expired": expired, "result": "Episodes added" if added_items else "No new episodes",
        "episode_titles": titles}
    new_file = not REPORT_CSV.exists() or REPORT_CSV.stat().st_size == 0
    with REPORT_CSV.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        if new_file:
            writer.writeheader()
        writer.writerow(row)


def append_success_report(now, added_items):
    """Append the compact success report used by the other podcast repositories."""
    SUCCESS_REPORT_CSV.parent.mkdir(parents=True, exist_ok=True)
    fields = ["date_tr", "time_tr", "added_count", "episode_titles"]
    local = now.astimezone(ZoneInfo("Europe/Istanbul"))
    if added_items:
        titles = " | ".join(
            f"{item.get('series_title') or 'Unknown mixshow'} — {item.get('title') or 'Untitled episode'}"
            for item in added_items
        )
    else:
        titles = "Yeni bölüm bulunamadı"
    row = {"date_tr": local.strftime("%d.%m.%Y"), "time_tr": local.strftime("%H:%M:%S"),
           "added_count": len(added_items), "episode_titles": titles}
    new_file = not SUCCESS_REPORT_CSV.exists() or SUCCESS_REPORT_CSV.stat().st_size == 0
    with SUCCESS_REPORT_CSV.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        if new_file:
            writer.writeheader()
        writer.writerow(row)


def main():
    if not USER or not PASSWORD:
        raise RuntimeError("SOURCE_USERNAME and SOURCE_PASSWORD GitHub Actions secrets are required.")
    if MAX_SERIES < 1:
        raise RuntimeError("MAX_DISCOVERED_SHOWS must be positive.")
    if EPISODES_PER_SHOW != 5:
        raise RuntimeError("This archive policy requires five newest episodes per mixshow.")

    now = datetime.now(timezone.utc)
    session = requests.Session()
    session.headers.update(HEADERS)
    configure_retries(session)
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
    except ImportError as exc:
        raise RuntimeError("Playwright is required for authenticated Download-button clicks.") from exc

    existing_items = read_existing_feed()
    # Immutable baseline: the final feed must retain every GUID published at run start.
    previous_published_items = list(existing_items)
    existing_by_guid = {canonical_url(item.get("guid") or item["page"]): item for item in existing_items}
    queue_at_start = read_retry_queue()
    queue = dict(queue_at_start)
    if REBUILD_ALL:
        log("full_rebuild_requested_preserving_history", existing_feed_items=len(existing_items),
            previous_retry_items=len(queue_at_start))

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        context = browser.new_context(user_agent=HEADERS["User-Agent"], accept_downloads=False)
        page = context.new_page()
        try:
            login_in_browser(page, context, session, PlaywrightTimeoutError)
            posts, catalog_rows = discover_episode_posts(session)
            catalog_by_slug = {row["show_slug"]: row for row in catalog_rows}
            groups = group_posts_by_series(posts)
            if not groups:
                raise RuntimeError("No mixshow archives with episode links were discovered.")

            show_rows, selected, run_added_items = [], [], []
            source_failures = 0
            for index, (show_key, show_posts) in enumerate(sorted(groups.items()), 1):
                show_title = str(show_posts[0].get("series_title") or show_key.replace("-", " ").title())
                stats = {
                    "show_slug": show_key, "show_title": show_title,
                    "source_episode_candidates": len(show_posts), "archive_status": ("fetch_error:" + str(catalog_by_slug[show_key]["archive_error"])) if catalog_by_slug.get(show_key, {}).get("archive_error") else "ok",
                    "episode_pages_selected": 0,
                    "added_this_run": 0, "already_in_feed": 0, "queued_failures": 0,
                    "latest_selected_title": "", "latest_selected_date": "", "selected_titles": "",
                }
                covered, examined, selected_titles = 0, 0, []

                # The show archive supplies newest-first episode links. Existing RSS
                # items count as already covered; unresolvable candidates enter the
                # retry CSV and we continue backwards until five usable episodes exist.
                for post in show_posts:
                    if covered >= EPISODES_PER_SHOW:
                        break
                    examined += 1
                    episode_url = canonical_url(post["link"])
                    guid = episode_url

                    if guid in existing_by_guid:
                        old_item = existing_by_guid[guid]
                        covered += 1
                        stats["already_in_feed"] += 1
                        selected_titles.append(old_item.get("title") or rest_title(post) or post["slug"])
                        if not stats["latest_selected_date"]:
                            stats["latest_selected_date"] = utc_iso(old_item["date"])
                        continue

                    # New queue entries are not hammered on every six-hour run.
                    # A due retry is handled once in the separate retry pass below.
                    if guid in queue and queue[guid].get("status", "waiting") == "waiting":
                        continue

                    item, error, is_episode = inspect_episode_post(session, post, show_key)
                    if not item:
                        if is_episode:
                            add_retry(queue, episode_url, show_key, show_title, rest_title(post) or post["slug"],
                                      rest_date(post), error or "episode_page_unavailable", now)
                            stats["queued_failures"] += 1
                            source_failures += 1
                        else:
                            log("archive_candidate_skipped", show=show_key, page=safe_url(episode_url),
                                reason=error or "not_an_episode_page")
                        if REQUEST_DELAY:
                            time.sleep(REQUEST_DELAY)
                        continue

                    item["series_key"] = show_key
                    item["series_title"] = item.get("series_title") or show_title
                    selected.append(item)
                    audio, length = click_download_and_resolve_audio(page, context, session, item, PlaywrightTimeoutError)
                    if audio:
                        item["audio"], item["audio_length"] = audio, length or 0
                        existing_by_guid[guid] = item
                        existing_items.append(item)
                        run_added_items.append(item)
                        covered += 1
                        stats["added_this_run"] += 1
                        selected_titles.append(item["title"])
                        if not stats["latest_selected_date"]:
                            stats["latest_selected_date"] = utc_iso(item["date"])
                        log("episode_added_to_archive", show=show_key, title=item["title"],
                            date=item["date"].isoformat(), page=safe_url(item["page"]),
                            audio_host=urlparse(audio).netloc, cover_found=bool(item.get("cover")),
                            covered_slots=covered)
                    else:
                        add_retry(queue, item["page"], show_key, item["series_title"], item["title"],
                                  item["date"], "download_click_or_audio_validation_failed", now)
                        stats["queued_failures"] += 1
                        source_failures += 1
                    if REQUEST_DELAY:
                        time.sleep(REQUEST_DELAY)

                stats["episode_pages_selected"] = covered
                stats["selected_titles"] = "; ".join(selected_titles)
                if selected_titles:
                    stats["latest_selected_title"] = selected_titles[0]
                show_rows.append(stats)

                if index == 1 or index % 10 == 0 or index == len(groups):
                    log("mixshow_archive_progress", checked=index, total_groups=len(groups),
                        episode_candidates=len(posts), selected_usable=covered,
                        rss_items=len(existing_items), retry_queue=len(queue))

            current_show_slugs = {row["show_slug"] for row in show_rows}
            for catalog_row in catalog_rows:
                slug = catalog_row["show_slug"]
                if slug in current_show_slugs:
                    continue
                archive_error = catalog_row.get("archive_error", "")
                show_rows.append({
                    "show_slug": slug,
                    "show_title": catalog_row.get("show_title") or slug.replace("-", " ").title(),
                    "source_episode_candidates": 0,
                    "archive_status": ("fetch_error:" + archive_error) if archive_error else "no_episode_links",
                    "episode_pages_selected": 0, "added_this_run": 0, "already_in_feed": 0,
                    "queued_failures": 0, "latest_selected_title": "", "latest_selected_date": "",
                    "selected_titles": "",
                })

            retry_attempted, recovered_count = 0, 0
            retries = [row for url, row in queue_at_start.items()
                       if url in queue and retry_is_due(queue[url], now)]
            for row in sorted(retries, key=lambda entry: entry.get("next_retry_utc", "")):
                url = canonical_url(row["episode_url"])
                current = queue.get(url)
                if current is None or current.get("status") != "waiting":
                    continue
                retry_attempted += 1
                episode_post = {
                    "link": url, "slug": urlparse(url).path.strip("/").split("/")[-1],
                    "date": current.get("source_date", ""), "date_gmt": current.get("source_date", ""),
                    "title": {"rendered": current.get("episode_title", "")},
                    "series_key": current.get("show_slug", ""), "series_title": current.get("show_title", ""),
                }
                item, error, is_episode = inspect_episode_post(session, episode_post, current.get("show_slug", ""))
                if not item:
                    if error == "not_an_episode_page":
                        queue.pop(url, None)
                        log("retry_queue_removed_not_episode", page=safe_url(url))
                    else:
                        update_retry_failure(current, error or "episode_page_unavailable", now)
                    continue

                audio, length = click_download_and_resolve_audio(page, context, session, item, PlaywrightTimeoutError)
                if not audio:
                    update_retry_failure(current, "download_click_or_audio_validation_failed", now)
                else:
                    item["audio"], item["audio_length"] = audio, length or 0
                    guid = canonical_url(item.get("guid") or item["page"])
                    was_in_feed = guid in existing_by_guid
                    if was_in_feed:
                        existing_by_guid[guid].update(item)
                    else:
                        existing_by_guid[guid] = item
                        existing_items.append(item)
                        run_added_items.append(item)
                    queue.pop(url, None)
                    recovered_count += 1
                    log("retry_queue_recovered", page=safe_url(url), title=item["title"],
                        added_to_rss=not was_in_feed)
                if REQUEST_DELAY:
                    time.sleep(REQUEST_DELAY)

            unique = {}
            for item in existing_items:
                guid = canonical_url(item.get("guid") or item.get("page", ""))
                if guid not in unique or item.get("date", datetime.min.replace(tzinfo=timezone.utc)) > unique[guid].get("date", datetime.min.replace(tzinfo=timezone.utc)):
                    item["guid"] = guid
                    item["page"] = canonical_url(item.get("page") or guid)
                    unique[guid] = item
            final_items = list(unique.values())
            if not final_items:
                raise RuntimeError("No validated episodes are available; production RSS was not written.")

            apply_series_metadata(final_items, catalog_rows, posts)
            canonicalize_series_covers(final_items)
            # Check before touching the production RSS file or tracking reports.
            validate_guid_retention(previous_published_items, final_items)
            write_rss(final_items)
            validate_feed(final_items)
            write_inventory(show_rows, now, final_items, posts)
            write_retry_queue(queue)
            append_update_report(now, REBUILD_ALL, len(posts), len(show_rows), len(selected),
                                 run_added_items, retry_attempted, recovered_count, queue, source_failures)
            append_success_report(now, run_added_items)
            log("archive_update_complete", run_type="full_rebuild" if REBUILD_ALL else "incremental",
                mixshows_discovered=len(show_rows), episode_candidates=len(posts),
                episode_pages_selected=len(selected), rss_items=len(final_items),
                added_count=len(run_added_items), retry_attempted=retry_attempted,
                recovered_count=recovered_count,
                retry_waiting=sum(1 for row in queue.values() if row.get("status") == "waiting"),
                retry_expired=sum(1 for row in queue.values() if row.get("status") == "expired"),
                source_failures=source_failures, rss_file=str(OUT))
        finally:
            browser.close()

if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log("fatal", error=f"{type(exc).__name__}: {exc}")
        sys.exit(1)
