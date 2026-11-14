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


def test_avoided_words_are_replaced_with_their_term_keeping_plurals_and_case(ctx):
    from sifu.glossary import Glossary, GlossaryTerm, normalize_text

    text = "A user adds two waypoints. Users see each waypoint."
    assert normalize_text(ctx.glossary, text) == (
        "A Traveller adds two Stops. Travellers see each Stop."
    )
    # a longer avoided phrase wins over a shorter one inside it
    glossary = Glossary(
        terms=[
            GlossaryTerm(term="Stop", definition="d", avoid=["point", "point of interest"]),
            GlossaryTerm(term="Day Plan", definition="d", avoid=["day schedule"]),
        ]
    )
    assert normalize_text(glossary, "Each point of interest on two day schedules.") == (
        "Each Stop on two Day Plans."
    )


def test_normalizing_leaves_code_spans_partial_words_and_clean_text_alone(ctx):
    from sifu.glossary import normalize_text

    text = "The username field and `location` key stay; a Traveller adds a Stop."
    assert normalize_text(ctx.glossary, text) == text


def test_normalizing_leaves_file_paths_alone(ctx):
    from sifu.glossary import normalize_text

    text = "Edit Sources/User/Waypoint.swift and user.yml for each user."
    assert normalize_text(ctx.glossary, text) == (
        "Edit Sources/User/Waypoint.swift and user.yml for each Traveller."
    )
