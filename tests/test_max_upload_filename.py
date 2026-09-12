"""Unit tests for MAX upload file-name encoding (src/max/client_pool.py).

Regression cover for 2026-09-12: a Telegram document named "расписание
уроков" reached MAX with a garbled name.  pymax sends the name raw inside the
``Content-Disposition`` header; the MAX web client percent-encodes it with
``encodeURIComponent`` and the server decodes that.
"""

from pymax.files import File as PyMaxFile

from src.max.client_pool import wire_filename


def test_cyrillic_name_matches_encodeURIComponent():
    # Reference value produced by encodeURIComponent("расписание уроков.xlsx")
    assert wire_filename("расписание уроков.xlsx") == (
        "%D1%80%D0%B0%D1%81%D0%BF%D0%B8%D1%81%D0%B0%D0%BD%D0%B8%D0%B5"
        "%20%D1%83%D1%80%D0%BE%D0%BA%D0%BE%D0%B2.xlsx"
    )


def test_ascii_name_is_untouched():
    assert wire_filename("report-2026_v1.pdf") == "report-2026_v1.pdf"


def test_encodeURIComponent_escape_set():
    # These stay literal in JS encodeURIComponent; everything else is escaped.
    assert wire_filename("a-b_c.d!e~f*g'h(i)j") == "a-b_c.d!e~f*g'h(i)j"
    assert wire_filename("a b&c/d+e") == "a%20b%26c%2Fd%2Be"


def test_pymax_file_keeps_encoded_name_whole():
    """Slashes are escaped, so Path(url).name inside pymax keeps the full name."""
    f = PyMaxFile(raw=b"x", url=wire_filename("папка/файл.txt"))
    assert f.file_name == "%D0%BF%D0%B0%D0%BF%D0%BA%D0%B0%2F%D1%84%D0%B0%D0%B9%D0%BB.txt"
