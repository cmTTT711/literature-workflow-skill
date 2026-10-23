import hashlib

from paper_acquisition.acquisition.naming import paper_target, pdf_filename
from paper_acquisition.acquisition.rename import migrate_pdf_names
from paper_acquisition.models import Paper
from paper_acquisition.storage import Catalog


def test_safe_names_and_stable_collisions(tmp_path):
    assert pdf_filename(2023, "A: B/C?") == "2023_A_ B_C_.pdf"
    assert len(pdf_filename(2024, "题" * 500).encode("utf-16-le")) // 2 <= 180
    c = Catalog(tmp_path)
    a = Paper(title="Identical", year=2023)
    b = Paper(title="Identical", year=2023)
    folder = tmp_path / "downloads/program/openalex"
    first = paper_target(c, folder, a)
    assert first.name == "2023_Identical.pdf"
    assert paper_target(c, folder, b).name == "2023_Identical_2.pdf"
    assert paper_target(c, folder, a) == first
    c.close()


def test_migration_preserves_agent_and_hash_and_is_idempotent(tmp_path):
    c = Catalog(tmp_path)
    paper = Paper(title="Magnetic materials", year=2023)
    c.save_paper(paper)
    old = tmp_path / "downloads/program/openalex/legacy.pdf"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"%PDF-example")
    agent = tmp_path / "downloads/agent/aps/unchanged.pdf"
    agent.parent.mkdir(parents=True)
    agent.write_bytes(b"agent-data")
    digest = hashlib.sha256(old.read_bytes()).hexdigest()
    c.db.execute("PRAGMA foreign_keys=OFF")
    c.execute(
        "INSERT INTO artifacts VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            "a",
            paper.paper_id,
            "attempt",
            "program",
            "VERIFIED",
            str(old.relative_to(tmp_path)),
            digest,
            old.stat().st_size,
            "now",
            "{}",
        ),
    )
    c.close()
    assert migrate_pdf_names(tmp_path)["rename_count"] == 1
    assert old.exists()
    result = migrate_pdf_names(tmp_path, apply=True)
    assert result["rename_count"] == 1
    c = Catalog(tmp_path)
    renamed = c.root / c.db.execute("SELECT path FROM artifacts").fetchone()[0]
    assert renamed.name == "2023_Magnetic materials.pdf"
    assert hashlib.sha256(renamed.read_bytes()).hexdigest() == digest
    assert agent.read_bytes() == b"agent-data"
    c.close()
    assert migrate_pdf_names(tmp_path)["rename_count"] == 0
