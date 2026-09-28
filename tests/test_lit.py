from __future__ import annotations

import importlib.util
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("literature_research_lit", ROOT / "scripts" / "lit.py")
assert SPEC and SPEC.loader
lit = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = lit
SPEC.loader.exec_module(lit)


def make_row(**overrides):
    values = {
        "title": "First-principles study of a solid electrolyte interface",
        "abstract": "Density functional theory reveals interfacial charge transfer in a solid-state battery.",
        "full_text": "Methods. Density functional theory was used. Results show charge transfer at the solid electrolyte interface.",
        "citation_count": 7,
        "pdf_path": "/tmp/example.pdf",
    }
    values.update(overrides)
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        "CREATE TABLE sample (title TEXT, abstract TEXT, full_text TEXT, citation_count INTEGER, pdf_path TEXT)"
    )
    connection.execute(
        "INSERT INTO sample VALUES (?, ?, ?, ?, ?)",
        tuple(values[key] for key in ("title", "abstract", "full_text", "citation_count", "pdf_path")),
    )
    return connection, connection.execute("SELECT * FROM sample").fetchone()


class GenericScreeningTests(unittest.TestCase):
    def setUp(self):
        self.profile = {
            "screening_mode": "generic",
            "search_terms": ["solid-state battery", "solid electrolyte interface"],
            "method_terms": ["density functional theory", "molecular dynamics"],
            "direction_terms": {"electronic_structure": ["density functional theory"]},
            "original_research_only": True,
        }

    def test_fulltext_record_can_be_included(self):
        connection, row = make_row()
        try:
            status, direction, method_primary, evidence, _ = lit.screen_generic_row(
                row, "fulltext", True, True, self.profile
            )
        finally:
            connection.close()
        self.assertEqual(status, "included")
        self.assertEqual(direction, "electronic_structure")
        self.assertEqual(method_primary, 1)
        self.assertTrue(evidence)

    def test_missing_pdf_stays_pending(self):
        connection, row = make_row(pdf_path=None, full_text="")
        try:
            status, *_ = lit.screen_generic_row(row, "abstract", True, True, self.profile)
        finally:
            connection.close()
        self.assertEqual(status, "pending_pdf")

    def test_explicit_exclusion_wins(self):
        profile = dict(self.profile, exclude_terms=["withdrawn"])
        connection, row = make_row(title="Withdrawn: first-principles solid-state battery study")
        try:
            status, *_ = lit.screen_generic_row(row, "abstract", True, True, profile)
        finally:
            connection.close()
        self.assertEqual(status, "excluded")

    def test_generic_report_is_english_and_evidence_labeled(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            (project_dir / "exports").mkdir()
            database = project_dir / "library.sqlite"
            lit.init_db(database)
            connection = lit.connect(database)
            try:
                connection.execute(
                    "INSERT INTO papers (id, doi, title, year, journal, citation_count, status, direction, "
                    "method_evidence, relevance_score, pdf_path, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        "paper-1",
                        "10.0000/example",
                        "Example interface paper",
                        2025,
                        "Example Journal",
                        3,
                        "included",
                        "electronic_structure",
                        "Density functional theory was used.",
                        9.0,
                        "/tmp/example.pdf",
                        lit.utc_now(),
                        lit.utc_now(),
                    ),
                )
                connection.commit()
                report_path = lit.write_report(
                    connection,
                    project_dir,
                    {**self.profile, "project": "test-project", "output_language": "en"},
                )
            finally:
                connection.close()
            report = report_path.read_text(encoding="utf-8")
            self.assertIn("Final PDF/full-text verified", report)
            self.assertIn("Evidence: Density functional theory was used.", report)


class PortabilityAndSecurityTests(unittest.TestCase):
    def test_default_output_is_outside_skill_tree(self):
        self.assertNotIn(lit.SKILL_ROOT, lit.DEFAULT_DATA_ROOT.parents)

    def test_webvpn_tls_verification_defaults_on(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertTrue(lit.webvpn_verify_tls())
        with mock.patch.dict(os.environ, {"LIT_WEBVPN_VERIFY_TLS": "false"}, clear=True):
            self.assertFalse(lit.webvpn_verify_tls())

    def test_profile_template_loads(self):
        profile = lit.read_mapping_file(ROOT / "assets" / "research_profile.yaml")
        self.assertEqual(profile["screening_mode"], "generic")
        self.assertTrue(profile["search_terms"])

    def test_publishable_files_do_not_contain_private_machine_path(self):
        excluded = {ROOT / "assets" / "local_runtime.yaml"}
        private_user_fragment = "124" + "00F"
        for path in ROOT.rglob("*"):
            if not path.is_file() or path in excluded or "data" in path.parts or ".git" in path.parts or "__pycache__" in path.parts:
                continue
            if path.suffix.lower() not in {".md", ".py", ".yaml", ".yml", ".txt", ".example", ""}:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            self.assertNotIn(private_user_fragment, text, str(path))


if __name__ == "__main__":
    unittest.main()

