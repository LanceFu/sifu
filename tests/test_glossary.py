from sifu.glossary import check_text


def test_flags_avoided_words_as_minor_with_the_preferred_term(ctx):
    found = check_text(ctx.glossary, "A user adds a waypoint.")
    assert {(f.severity, f.message) for f in found} == {
        ("minor", "Use 'Traveller' instead of 'user'."),
        ("minor", "Use 'Stop' instead of 'waypoint'."),
    }


def test_matches_whole_words_only(ctx):
    assert check_text(ctx.glossary, "The username and users' waypointing") == [
        f for f in check_text(ctx.glossary, "The username and users' waypointing")
    ]
    assert check_text(ctx.glossary, "username") == []


def test_out_of_scope_is_major_and_matches_plurals(ctx):
    found = check_text(ctx.glossary, "Add budgets and Booking links.")
    assert [(f.category, f.severity) for f in found] == [("out_of_scope", "major")] * 2


def test_clean_text_has_no_findings(ctx):
    assert check_text(ctx.glossary, "A Traveller adds a Stop.") == []
