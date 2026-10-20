import unittest
from datetime import timezone
from bs4 import BeautifulSoup
import build_feed

class FeedTests(unittest.TestCase):
    def test_audio_extracts_encoded_direct_mp3(self):
        soup = BeautifulSoup('<audio controls><source src="https://files.topdjmixes.com/Resident/Hernan%20Cattaneo%20-%20Resident%20Episode%20805_VPtDbeR7vVpTSTL.mp3" type="audio/mpeg"></audio>', "html.parser")
        url = build_feed.find_audio(soup, "https://www.topdjmixes.com/resident-805/")
        self.assertEqual(url, "https://files.topdjmixes.com/Resident/Hernan%20Cattaneo%20-%20Resident%20Episode%20805_VPtDbeR7vVpTSTL.mp3")

    def test_detects_show_link(self):
        soup = BeautifulSoup('<a href="/resident-805/">Hernan Cattaneo Resident Episode 805</a>', "html.parser")
        links = build_feed.extract_show_links(soup, "https://www.topdjmixes.com/mix-shows/")
        self.assertIn("https://www.topdjmixes.com/resident-805/", links)

    def test_ignores_non_mp3(self):
        soup = BeautifulSoup('<audio src="https://example.com/file.m3u8"></audio>', "html.parser")
        self.assertIsNone(build_feed.find_audio(soup, "https://example.com/"))

    def test_success_report_zero_uses_turkish_message(self):
        from tempfile import TemporaryDirectory
        from pathlib import Path
        from datetime import datetime, timezone
        with TemporaryDirectory() as tmp:
            original = build_feed.SUCCESS_REPORT_CSV
            try:
                build_feed.SUCCESS_REPORT_CSV = Path(tmp) / "success-report.csv"
                build_feed.append_success_report(datetime(2026, 10, 20, tzinfo=timezone.utc), [])
                content = build_feed.SUCCESS_REPORT_CSV.read_text(encoding="utf-8")
                self.assertIn("added_count,episode_titles", content)
                self.assertIn("0,Yeni bölüm bulunamadı", content)
            finally:
                build_feed.SUCCESS_REPORT_CSV = original

    def test_read_existing_feed_keeps_history_when_rebuild_requested(self):
        from tempfile import TemporaryDirectory
        from pathlib import Path
        original_out, original_rebuild = build_feed.OUT, build_feed.REBUILD_ALL
        try:
            with TemporaryDirectory() as tmp:
                build_feed.OUT = Path(tmp) / "rss.xml"
                build_feed.OUT.write_text('''<?xml version="1.0"?><rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"><channel><item><title>Older Show 1</title><link>https://www.topdjmixes.com/older-show-1/</link><guid>https://www.topdjmixes.com/older-show-1/</guid><pubDate>Tue, 20 Oct 2026 00:00:00 GMT</pubDate><description>old</description><itunes:image href="https://example.com/cover.jpg"/><enclosure url="https://files.topdjmixes.com/a.mp3" length="10" type="audio/mpeg"/></item></channel></rss>''', encoding="utf-8")
                build_feed.REBUILD_ALL = True
                items = build_feed.read_existing_feed()
                self.assertEqual(len(items), 1)
                self.assertEqual(items[0]["title"], "Older Show 1")
        finally:
            build_feed.OUT, build_feed.REBUILD_ALL = original_out, original_rebuild

    def test_inventory_preserves_shows_missing_from_current_catalogue(self):
        from tempfile import TemporaryDirectory
        from pathlib import Path
        from datetime import datetime, timezone
        import csv
        original_path, original_rebuild = build_feed.INVENTORY_CSV, build_feed.REBUILD_ALL
        try:
            with TemporaryDirectory() as tmp:
                build_feed.INVENTORY_CSV = Path(tmp) / "show-inventory.csv"
                build_feed.REBUILD_ALL = True
                build_feed.INVENTORY_CSV.write_text(
                    'run_at_utc,show_slug,show_title,source_episode_candidates,episode_pages_selected,added_this_run,already_in_feed,queued_failures,latest_selected_title,latest_selected_date,selected_titles,episodes_for_radioshow\n'
                    '2026-10-19T12:00:00+00:00,old-show,Old Show,2,1,0,1,0,Old Show 2,2026-10-18T00:00:00+00:00,Old Show 2; Old Show 1,"- [Old Show 2](https://www.topdjmixes.com/old-show-2/)\n- [Old Show 1](https://www.topdjmixes.com/old-show-1/)"\n',
                    encoding="utf-8")
                build_feed.write_inventory([], datetime(2026, 10, 20, tzinfo=timezone.utc), [])
                with build_feed.INVENTORY_CSV.open(encoding="utf-8", newline="") as handle:
                    rows = list(csv.DictReader(handle))
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["show_slug"], "old-show")
                self.assertIn("Old Show 1", rows[0]["episodes_for_radioshow"])
        finally:
            build_feed.INVENTORY_CSV, build_feed.REBUILD_ALL = original_path, original_rebuild

    def test_inventory_appends_discovered_archive_links_not_in_rss(self):
        from tempfile import TemporaryDirectory
        from pathlib import Path
        from datetime import datetime, timezone
        import csv
        original_path = build_feed.INVENTORY_CSV
        try:
            with TemporaryDirectory() as tmp:
                build_feed.INVENTORY_CSV = Path(tmp) / "show-inventory.csv"
                posts = [
                    {"link": "https://www.topdjmixes.com/old-show-3/", "slug": "old-show-3",
                     "title": {"rendered": "Old Show 3"}, "series_key": "old-show",
                     "series_title": "Old Show"},
                    {"link": "https://www.topdjmixes.com/old-show-2/", "slug": "old-show-2",
                     "title": {"rendered": "Old Show 2"}, "series_key": "old-show",
                     "series_title": "Old Show"},
                ]
                build_feed.write_inventory(
                    [{"show_slug": "old-show", "show_title": "Old Show", "source_episode_candidates": 2,
                      "selected_titles": "Old Show 3"}],
                    datetime(2026, 10, 20, tzinfo=timezone.utc), [], posts)
                with build_feed.INVENTORY_CSV.open(encoding="utf-8", newline="") as handle:
                    rows = list(csv.DictReader(handle))
                self.assertEqual(len(rows), 1)
                links = rows[0]["episodes_for_radioshow"]
                self.assertIn("https://www.topdjmixes.com/old-show-3/", links)
                self.assertIn("https://www.topdjmixes.com/old-show-2/", links)
        finally:
            build_feed.INVENTORY_CSV = original_path

    def test_cover_extractor_matches_punctuationless_sunsets_filename(self):
        soup = BeautifulSoup(
            '<article><div class="entry-content">'
            '<img src="https://www.topdjmixes.com/wp-content/uploads/2026/08/SunSets-1250x569.webp" '
            'alt="SunSets" width="1250" height="569">'
            '</div></article>', "html.parser")
        cover = build_feed.extract_cover(
            soup, "https://www.topdjmixes.com/sunsets-612/", "Sun:Sets 612")
        self.assertEqual(cover, "https://www.topdjmixes.com/wp-content/uploads/2026/08/SunSets-1250x569.webp")

    def test_cover_url_liveness_rejects_http_404(self):
        class FakeResponse:
            status_code = 404
            headers = {"Content-Type": "text/html; charset=UTF-8"}
            def close(self):
                pass
        class FakeSession:
            def get(self, *args, **kwargs):
                return FakeResponse()
        original_cache = build_feed.COVER_URL_VALIDITY_CACHE
        try:
            build_feed.COVER_URL_VALIDITY_CACHE = {}
            self.assertFalse(build_feed.cover_url_is_live(
                FakeSession(), "https://www.topdjmixes.com/wp-content/uploads/old-cover.jpg"))
        finally:
            build_feed.COVER_URL_VALIDITY_CACHE = original_cache

    def test_missing_episode_covers_fall_back_to_podcast_cover(self):
        rows = [{"title": "Sun:Sets", "series_key": "sun-sets", "cover": None}]
        build_feed.canonicalize_series_covers(rows)
        self.assertEqual(rows[0]["cover"], build_feed.PODCAST_COVER_URL)

    def test_equivalent_cover_formats_use_jpeg_without_changing_episode_specific_art(self):
        rows = [
            {"title": "Octopus Trance Radio 100", "series_key": "octopus-trance-radio",
             "cover": "https://www.topdjmixes.com/wp-content/uploads/2024/08/Octopus-Trance-Radio.jpg"},
            {"title": "Octopus Trance Radio 101", "series_key": "octopus-trance-radio",
             "cover": "https://www.topdjmixes.com/wp-content/uploads/2026/08/Octopus-Trance-Radio.webp"},
            {"title": "The Anjunabeats Rising Residency 114", "series_key": "the-anjunabeats-rising-residency",
             "cover": "https://www.topdjmixes.com/wp-content/uploads/2024/08/The-Anjunabeats-Rising-Residency-114-1.jpg"},
            {"title": "The Anjunabeats Rising Residency 113", "series_key": "the-anjunabeats-rising-residency",
             "cover": "https://www.topdjmixes.com/wp-content/uploads/2024/08/The-Anjunabeats-Rising-Residency-113-1.jpg"},
        ]
        build_feed.canonicalize_series_covers(rows)
        self.assertTrue(rows[0]["cover"].endswith(".jpg"))
        self.assertEqual(rows[0]["cover"], rows[1]["cover"])
        self.assertNotEqual(rows[2]["cover"], rows[3]["cover"])

    def test_rss_channel_artwork_and_metadata_are_unique(self):
        from tempfile import TemporaryDirectory
        from pathlib import Path
        from datetime import datetime, timezone
        import xml.etree.ElementTree as ET
        original_out = build_feed.OUT
        try:
            with TemporaryDirectory() as tmp:
                build_feed.OUT = Path(tmp) / "rss.xml"
                build_feed.write_rss([{
                    "title": "Test Episode",
                    "page": "https://www.topdjmixes.com/test-episode/",
                    "guid": "https://www.topdjmixes.com/test-episode/",
                    "date": datetime(2026, 10, 20, tzinfo=timezone.utc),
                    "description": "Test audio",
                    "cover": build_feed.PODCAST_COVER_URL,
                    "audio": "https://files.topdjmixes.com/test-episode.mp3",
                    "audio_length": 100,
                }])
                root = ET.parse(build_feed.OUT).getroot()
                channel = root.find("./channel")
                ns = build_feed.ITUNES_NS
                self.assertEqual(len(channel.findall(f"{{{ns}}}author")), 1)
                self.assertEqual(len(channel.findall(f"{{{ns}}}category")), 1)
                images = channel.findall(f"{{{ns}}}image")
                self.assertEqual(len(images), 1)
                self.assertEqual(images[0].get("href"), build_feed.PODCAST_COVER_URL)
                item = channel.find("item")
                self.assertIsNotNone(item.find(f"{{{ns}}}image"))
        finally:
            build_feed.OUT = original_out

    def test_missing_published_episode_is_queued_for_every_update(self):
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        old = [{"guid": "https://www.topdjmixes.com/old-episode/",
                "page": "https://www.topdjmixes.com/old-episode/",
                "title": "Old episode", "series_key": "test-show",
                "series_title": "Test Show", "date": now - timedelta(days=500)}]
        current = [{"guid": "https://www.topdjmixes.com/new-episode/",
                    "page": "https://www.topdjmixes.com/new-episode/"}]
        queue = {}
        missing = build_feed.queue_missing_published_items(old, current, queue, now)
        key = "https://www.topdjmixes.com/old-episode/"
        self.assertEqual(len(missing), 1)
        self.assertEqual(queue[key]["last_error"], "previously_published_guid_missing_from_current_archive")
        self.assertEqual(queue[key]["retry_every_update"], "true")
        self.assertTrue(build_feed.retry_is_due(queue[key], now + timedelta(days=60)))

    def test_missing_published_episode_not_queued_if_still_in_rss(self):
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        item = {"guid": "https://www.topdjmixes.com/old-episode/",
                "page": "https://www.topdjmixes.com/old-episode/"}
        queue = {}
        missing = build_feed.queue_missing_published_items([item], [item], queue, now)
        self.assertEqual(missing, [])
        self.assertEqual(queue, {})

    def test_xml_escapes_text(self):
        self.assertEqual(build_feed.xml_escape('A & B "C"'), "A &amp; B &quot;C&quot;")

    def test_xml_description_keeps_line_breaks(self):
        description = "Size: 274 Mb\\n\\n\\nTracklist:\\nFirst track"
        self.assertEqual(build_feed.xml_escape(description), description)

    def test_description_preserves_line_breaks_and_adds_two_blank_lines(self):
        source = "Size: 274 Mb\nTracklist:\n1. First track\n2. Second track"
        expected = "Size: 274 Mb\n\n\nTracklist:\n1. First track\n2. Second track"
        self.assertEqual(build_feed.format_description(source), expected)

    def test_description_adds_two_blank_lines_when_size_and_tracklist_share_a_line(self):
        source = "Intro\nSize: 274 Mb Tracklist:\n1. First track\n2. Second track"
        expected = "Intro\nSize: 274 Mb\n\n\nTracklist:\n1. First track\n2. Second track"
        self.assertEqual(build_feed.format_description(source), expected)

    def test_extract_description_prefers_page_content_with_size_and_tracklist(self):
        soup = BeautifulSoup(
            '<article><div class="entry-content"><p>Show details</p>'
            '<p>Size: 274 Mb</p><h3>Tracklist:</h3><ol><li>First track</li>'
            '<li>Second track</li></ol></div></article>', "html.parser")
        description = build_feed.extract_description(soup)
        self.assertIn("Size: 274 Mb\n\n\nTracklist:", description)
        self.assertIn("First track", description)
        self.assertIn("Second track", description)

if __name__ == "__main__":
    unittest.main()
