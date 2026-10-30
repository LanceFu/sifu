from pathlib import Path

import pytest

from sifu import db
from sifu.config import load_config
from sifu.context import ProductContext
from sifu.glossary import Glossary, GlossaryTerm

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def ctx():
    glossary = Glossary(
        terms=[
            GlossaryTerm(term="Traveller", definition="The person using the app.", avoid=["user"]),
            GlossaryTerm(term="Stop", definition="One planned visit.", avoid=["waypoint"]),
        ],
        out_of_scope=["Booking", "Budget"],
    )
    return ProductContext(
        glossary, "| REQ-1 | Trip needs a name | Implemented |", "Use the glossary."
    )


@pytest.fixture()
def cfg():
    return load_config(ROOT / "config.yaml")


@pytest.fixture()
def conn():
    connection = db.connect(":memory:")
    db.init_db(connection)
    yield connection
    connection.close()


@pytest.fixture()
def make_ticket(conn):
    project_id = db.create_project(conn, "TripPlanner", "/tmp/tripplanner-ios", "owner/repo")

    def _make(title, body):
        return db.create_ticket(conn, project_id, title, body)

    return _make


@pytest.fixture()
def make_deps(conn, cfg, ctx, tmp_path):
    def _make(llm):
        from sifu.graph import Deps

        return Deps(conn=conn, llm=llm, cfg=cfg, ctx=ctx, artifacts_dir=tmp_path / "artifacts")

    return _make
