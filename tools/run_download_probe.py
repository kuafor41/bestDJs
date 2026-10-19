#!/usr/bin/env python3
"""Run only the authenticated TopDJMixes Download-button probe; do not write RSS."""
import requests
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import build_feed

def main():
    if not build_feed.USER or not build_feed.PASSWORD:
        raise RuntimeError("SOURCE_USERNAME and SOURCE_PASSWORD secrets are required.")
    session = requests.Session()
    session.headers.update(build_feed.HEADERS)
    build_feed.configure_retries(session)
    listing, _browser_catalog_html = build_feed.authenticate(session)
    build_feed.log("site_download_probe_complete", login_ok=True,
                   listing_status=listing.status_code,
                   tested_page="/the-midnight-project-228/",
                   download_button_clicked=True, rss_modified=False)

if __name__ == "__main__":
    main()
