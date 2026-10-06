import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from omero_webimport.filenames import safe_filename  # noqa: E402

"""
Unit tests for upload filename sanitising (pure - no OMERO or Django).

The directory cases must hold on every OS: before the fix they passed on a
Windows dev machine but not on the Linux server.
"""


def test_strips_posix_and_windows_directories():
    """
    Drop directory parts written with either separator.
    """
    assert safe_filename("a b/c\\d.pdf") == "d.pdf"
    assert safe_filename("C:\\Users\\x\\slide.svs") == "slide.svs"
    assert safe_filename("../../evil.sh") == "evil.sh"


def test_replaces_unsafe_characters_and_caps_length():
    """
    Replace each run of unsafe characters and keep at most 200.
    """
    assert safe_filename("slide #1 (copy).svs") == "slide _1 _copy_.svs"
    assert len(safe_filename("x" * 300 + ".tif")) == 200


def test_empty_or_missing_name():
    """
    Fall back to "unnamed" when no basename is left.
    """
    assert safe_filename("") == "unnamed"
    assert safe_filename(None) == "unnamed"
    assert safe_filename("C:\\Users\\x\\") == "unnamed"
