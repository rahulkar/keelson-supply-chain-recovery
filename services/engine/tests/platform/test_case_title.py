from keelson_engine.api.routes_cases import case_title_from


def test_short_text_is_kept_whole() -> None:
    assert case_title_from("  What if Suez closes?  ") == "What if Suez closes?"


def test_long_text_is_cut_at_a_word_boundary_with_ellipsis() -> None:
    text = ("Our trim supplier Meghna Trims says wadding and zips for the Bangladesh POs "
            "are delayed 7 days. What is the impact?")
    title = case_title_from(text)
    assert title.endswith("\u2026")
    assert len(title) <= 73
    assert not title[:-1].endswith(" ")
    assert text.startswith(title[:-1])
    assert title[:-1].split(" ")[-1] in text.split(" ")  # last word is whole


def test_only_first_line_is_used() -> None:
    assert case_title_from("Port strike in Antwerp\nmore details here") == "Port strike in Antwerp"
