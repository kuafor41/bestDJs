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

    def test_xml_escapes_text(self):
        self.assertEqual(build_feed.xml_escape('A & B "C"'), "A &amp; B &quot;C&quot;")

    def test_description_preserves_line_breaks_and_separates_size_from_tracklist(self):
        source = "Size: 274 Mb\nTracklist:\n1. First track\n2. Second track"
        expected = "Size: 274 Mb\n\nTracklist:\n1. First track\n2. Second track"
        self.assertEqual(build_feed.format_description(source), expected)

    def test_description_adds_separator_when_size_and_tracklist_share_a_line(self):
        source = "Intro\nSize: 274 Mb Tracklist:\n1. First track\n2. Second track"
        expected = "Intro\nSize: 274 Mb\n\nTracklist:\n1. First track\n2. Second track"
        self.assertEqual(build_feed.format_description(source), expected)

if __name__ == "__main__":
    unittest.main()
