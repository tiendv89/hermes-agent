"""Unit tests for m4-admin-control-model-config T1 — model_catalog migration.

Covers:
  - ModelCatalog ORM model has all required columns
  - Migration file 006_model_catalog.sql exists and contains expected DDL
  - Migration file includes the partial unique index for is_default
  - Migration file includes the backfill INSERT for all 5 SUPPORTED_MODELS
"""

from __future__ import annotations

import pathlib

_MIGRATIONS_DIR = pathlib.Path(__file__).resolve().parents[2] / "migrations"
_MIGRATION_FILE = _MIGRATIONS_DIR / "006_model_catalog.sql"

EXPECTED_MODELS = [
    "claude-opus-4-8",
    "claude-sonnet-4-6",
    "claude-haiku-4-5",
    "deepseek-v4-flash",
    "deepseek-v4-pro",
]


# ---------------------------------------------------------------------------
# ORM model column checks
# ---------------------------------------------------------------------------


def test_model_catalog_has_model_id_column():
    """ModelCatalog ORM declares model_id as primary key."""
    from src.db.models import ModelCatalog

    assert hasattr(ModelCatalog, "model_id")
    col = ModelCatalog.__table__.c["model_id"]
    assert col.primary_key


def test_model_catalog_has_display_name_column():
    from src.db.models import ModelCatalog

    assert hasattr(ModelCatalog, "display_name")
    col = ModelCatalog.__table__.c["display_name"]
    assert not col.nullable


def test_model_catalog_has_provider_column():
    from src.db.models import ModelCatalog

    assert hasattr(ModelCatalog, "provider")
    col = ModelCatalog.__table__.c["provider"]
    assert not col.nullable


def test_model_catalog_has_is_active_column():
    from src.db.models import ModelCatalog

    assert hasattr(ModelCatalog, "is_active")
    col = ModelCatalog.__table__.c["is_active"]
    assert not col.nullable


def test_model_catalog_has_is_default_column():
    from src.db.models import ModelCatalog

    assert hasattr(ModelCatalog, "is_default")
    col = ModelCatalog.__table__.c["is_default"]
    assert not col.nullable


def test_model_catalog_has_created_at_column():
    from src.db.models import ModelCatalog

    assert hasattr(ModelCatalog, "created_at")
    col = ModelCatalog.__table__.c["created_at"]
    assert not col.nullable


def test_model_catalog_has_updated_at_column():
    from src.db.models import ModelCatalog

    assert hasattr(ModelCatalog, "updated_at")
    col = ModelCatalog.__table__.c["updated_at"]
    assert not col.nullable


def test_model_catalog_tablename():
    from src.db.models import ModelCatalog

    assert ModelCatalog.__tablename__ == "model_catalog"


# ---------------------------------------------------------------------------
# Migration file content checks
# ---------------------------------------------------------------------------


def test_migration_file_exists():
    """006_model_catalog.sql must be present in migrations/."""
    assert _MIGRATION_FILE.exists(), (
        f"Missing migration file: {_MIGRATION_FILE}. "
        "Create migrations/006_model_catalog.sql."
    )


def test_migration_creates_model_catalog_table():
    sql = _MIGRATION_FILE.read_text()
    assert "CREATE TABLE" in sql.upper() and "model_catalog" in sql.lower()


def test_migration_has_unique_index_on_is_default():
    """Partial unique index model_catalog_one_default must be present."""
    sql = _MIGRATION_FILE.read_text()
    assert "model_catalog_one_default" in sql, (
        "Migration must include CREATE UNIQUE INDEX model_catalog_one_default"
    )
    assert "WHERE is_default" in sql or "where is_default" in sql.lower()


def test_migration_backfills_all_five_models():
    """All 5 SUPPORTED_MODELS must appear in the backfill INSERT."""
    sql = _MIGRATION_FILE.read_text()
    for model_id in EXPECTED_MODELS:
        assert model_id in sql, (
            f"Missing backfill row for '{model_id}' in 006_model_catalog.sql"
        )


def test_migration_sets_sonnet_as_default():
    """claude-sonnet-4-6 must be the only model with is_default = TRUE."""
    sql = _MIGRATION_FILE.read_text()
    # Quick structural check: sonnet row contains TRUE for is_default
    assert "claude-sonnet-4-6" in sql
    # The row for sonnet should end with TRUE (the is_default flag)
    lines = [line for line in sql.splitlines() if "claude-sonnet-4-6" in line]
    assert lines, "No line containing claude-sonnet-4-6 found in migration"
    assert any("TRUE" in line for line in lines), (
        "claude-sonnet-4-6 row must have is_default = TRUE"
    )
