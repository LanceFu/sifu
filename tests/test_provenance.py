"""The git stamp that ties an eval result to the code and product context that produced it."""

import subprocess

from sifu.provenance import finish_stamp, git_stamp


def git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def make_repo(tmp_path):
    git("init", "-q", cwd=tmp_path)
    git("config", "user.email", "t@example.com", cwd=tmp_path)
    git("config", "user.name", "t", cwd=tmp_path)
    (tmp_path / "a.md").write_text("one\n")
    git("add", ".", cwd=tmp_path)
    git("commit", "-q", "-m", "init", cwd=tmp_path)
    return tmp_path


def test_a_clean_tree_records_its_commit_and_is_not_dirty(tmp_path):
    repo = make_repo(tmp_path)
    stamp = git_stamp(repo)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True
    ).stdout.strip()
    assert stamp["commit"] == head and stamp["dirty"] is False and stamp["dirty_files"] == []


def test_edited_and_new_files_make_the_tree_dirty_and_change_the_fingerprint(tmp_path):
    repo = make_repo(tmp_path)
    clean = git_stamp(repo)
    (repo / "a.md").write_text("two\n")
    edited = git_stamp(repo)
    assert edited["dirty"] and edited["dirty_files"] == ["a.md"]
    assert edited["fingerprint"] != clean["fingerprint"]
    (repo / "new.md").write_text("x\n")
    added = git_stamp(repo)
    assert added["dirty_files"] == ["a.md", "new.md"]
    (repo / "new.md").write_text("y\n")  # an untracked file's contents count too
    assert git_stamp(repo)["fingerprint"] != added["fingerprint"]


def test_a_change_during_the_run_is_flagged(tmp_path):
    repo = make_repo(tmp_path)
    start = git_stamp(repo)
    assert finish_stamp(start, repo)["changed_during_run"] is False
    (repo / "a.md").write_text("edited mid-run\n")
    done = finish_stamp(start, repo)
    assert done["changed_during_run"] is True and done["commit"] == start["commit"]


def test_a_folder_outside_git_gives_an_empty_stamp(tmp_path):
    stamp = git_stamp(tmp_path)
    assert stamp["commit"] is None and stamp["dirty"] is None
    assert finish_stamp(stamp, tmp_path)["changed_during_run"] is None
