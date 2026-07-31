"""
Tests for the folder-name parsing logic. Run with: pytest
"""

from src.crawler.crawl import parse_job_folder, parse_site_folder


def test_parse_job_folder():
    assert parse_job_folder("22-007 AYTO TORRENT") == ("22-007", "AYTO TORRENT")
    assert parse_job_folder("random name") is None


def test_parse_site_folder():
    assert parse_site_folder("22-007-02 CALLE SAN LUIS BELTRAN") == (
        "22-007-02",
        "CALLE SAN LUIS BELTRAN",
    )
    assert parse_site_folder("22-007 AYTO TORRENT") is None
