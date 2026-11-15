from sifu.lessons import append_lesson, propose_lesson

BASE = "# Conventions\n\n## Build\n\n- swift test\n\n## Lessons\n\n<!-- Sifu appends here -->\n"


def test_lessons_are_appended_under_the_heading_and_deduplicated(tmp_path):
    path = tmp_path / "conventions.md"
    path.write_text(BASE)
    assert append_lesson(path, "Never add  dependencies.", ticket_id=4)
    assert not append_lesson(path, "Never add dependencies.", ticket_id=4)
    text = path.read_text()
    assert text.endswith("- (ticket 4) Never add dependencies.\n")
    assert text.count("Never add dependencies") == 1


def test_missing_heading_is_created(tmp_path):
    path = tmp_path / "c.md"
    path.write_text("# Conventions\n")
    append_lesson(path, "Use Traveller.")
    assert "## Lessons\n- Use Traveller." in path.read_text()


def test_propose_lesson_leaves_the_approved_file_untouched(tmp_path):
    approved, proposed = tmp_path / "conventions.md", tmp_path / "out" / "conventions.md"
    approved.write_text(BASE)
    propose_lesson(approved, proposed, "Spec edits need tests.", ticket_id=2)
    assert approved.read_text() == BASE
    assert "Spec edits need tests." in proposed.read_text() and "swift test" in proposed.read_text()
