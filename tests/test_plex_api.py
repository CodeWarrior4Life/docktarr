from pathlib import Path
import pytest
from doctarr.plex_api import PlexPreferences, parse_preferences_xml


FIXTURES = Path(__file__).parent / "fixtures"


def test_parse_preferences_extracts_token_and_hw_flag():
    xml = (FIXTURES / "plex_preferences_valid.xml").read_text()
    prefs = parse_preferences_xml(xml)
    assert prefs.token == "xAbCdEfGhIjKlMnOpQrS"
    assert prefs.get("HardwareAcceleratedCodecs") == "1"
    assert prefs.get("TranscoderTempDirectory") == "/transcode"


def test_parse_preferences_missing_hw_flag_returns_none():
    xml = (FIXTURES / "plex_preferences_no_hw.xml").read_text()
    prefs = parse_preferences_xml(xml)
    assert prefs.token == "xAbCdEfGhIjKlMnOpQrS"
    assert prefs.get("HardwareAcceleratedCodecs") is None
