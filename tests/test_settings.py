"""Run options a person can override per database, and nothing else."""

import pytest

from sifu import db, settings


@pytest.fixture()
def path(tmp_path):
    file = tmp_path / "s.db"
    conn = db.connect(file)
    db.init_db(conn)
    conn.close()
    return file


def test_a_saved_option_is_loaded_and_applied_to_a_copy_of_the_config(path, cfg):
    cfg.router.merge_mode, cfg.critic.enabled = "risk_based", False
    settings.save(path, "router.merge_mode", "manual")
    settings.save(path, "critic.enabled", True)
    assert settings.load(path) == {"router.merge_mode": "manual", "critic.enabled": True}
    applied = settings.apply(cfg, settings.load(path))
    assert (applied.router.merge_mode, applied.critic.enabled) == ("manual", True)
    assert (cfg.router.merge_mode, cfg.critic.enabled) == ("risk_based", False)


def test_clearing_an_option_goes_back_to_the_config_file(path, cfg):
    settings.save(path, "planner.enabled", True)
    settings.clear(path, "planner.enabled")
    assert settings.load(path) == {}


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("router.protected_paths", []),
        ("models.author", "other"),
        ("router.max_tokens_per_ticket", 1),
        ("router.merge_mode", "yolo"),
        ("planner.enabled", "yes"),
        ("planner.enabled", 1),
    ],
)
def test_only_the_four_options_and_their_values_can_be_saved(path, key, value):
    with pytest.raises(ValueError):
        settings.save(path, key, value)
    assert settings.load(path) == {}


def test_apply_ignores_keys_it_does_not_know(cfg):
    applied = settings.apply(cfg, {"router.protected_paths": [], "models.author": "x"})
    assert applied.router.protected_paths == cfg.router.protected_paths
    assert applied.models.author == cfg.models.author


def test_load_is_empty_and_writes_nothing_without_the_table_or_the_file(path, tmp_path):
    conn = db.connect(path)
    conn.execute("DROP TABLE settings")
    conn.commit()
    conn.close()
    before = path.read_bytes()
    assert settings.load(path) == {} and path.read_bytes() == before
    assert settings.load(tmp_path / "missing.db") == {}
    assert not (tmp_path / "missing.db").exists()
