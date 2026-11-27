"""Unit tests for institutional_masterbook."""

import json
import tempfile
import unittest
from pathlib import Path

from institutional_masterbook import (
    compute_sha256,
    load_dotenv_if_present,
    main,
    persist_verified_probes,
    run_masterbook,
)


class TestMasterbook(unittest.TestCase):
    """Verify hashing, dry-run execution, driver dispatch, and certificate generation."""

    def test_compute_sha256_is_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            file_a = tmp_dir / "a.txt"
            file_b = tmp_dir / "b.txt"
            file_a.write_text("hello", encoding="utf-8")
            file_b.write_text("world", encoding="utf-8")

            digest_1 = compute_sha256([file_a, file_b])
            digest_2 = compute_sha256([file_b, file_a])
            self.assertEqual(digest_1, digest_2)
            self.assertEqual(len(digest_1), 64)

    def test_dry_run_generates_certificate_for_all_enabled(self) -> None:
        project_dir = Path(__file__).parent
        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "publication_certificate.json"
            cert = run_masterbook(
                registry_path=project_dir / "endpoints_registry.json",
                metadata_path=project_dir / "package_metadata.example.json",
                output_path=out_path,
                selected_institutions=None,
                use_sandbox=True,
                dry_run=True,
            )
            self.assertTrue(out_path.is_file())
            loaded = json.loads(out_path.read_text(encoding="utf-8"))
            self.assertEqual(loaded["masterbook_version"], "2.4.0")
            self.assertEqual(loaded["local_artifact_sha256"], cert["local_artifact_sha256"])
            statuses = {c["institution"]: c["status"] for c in loaded["certifications"]}
            for name in (
                "Harvard Dataverse",
                "Zenodo (CERN)",
                "OSF (Open Science Framework)",
                "Figshare",
                "Dryad",
                "Internet Archive",
                "Software Heritage (UNESCO)",
            ):
                self.assertEqual(statuses[name], "DRY_RUN")

    def test_dataverse_driver_reuse_and_query_fallback(self) -> None:
        project_dir = Path(__file__).parent
        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "cert_subset.json"
            cert = run_masterbook(
                registry_path=project_dir / "endpoints_registry.json",
                metadata_path=project_dir / "package_metadata.example.json",
                output_path=out_path,
                selected_institutions=["edmond_mpg", "re3data"],
                use_sandbox=False,
                dry_run=True,
            )
            statuses = {c["institution"]: c["status"] for c in cert["certifications"]}
            self.assertEqual(statuses["Edmond (Max-Planck-Gesellschaft)"], "DRY_RUN")
            self.assertEqual(statuses["re3data.org"], "MANUAL_OR_QUERY_ONLY")

    def test_dataverse_space_in_package_metadata_dry_run(self) -> None:
        project_dir = Path(__file__).parent
        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "cert_space.json"
            cert = run_masterbook(
                registry_path=project_dir / "endpoints_registry.json",
                metadata_path=project_dir / "package_metadata.json",
                output_path=out_path,
                selected_institutions=["harvard_dataverse"],
                use_sandbox=True,
                dry_run=True,
            )
            dv = cert["certifications"][0]
            self.assertEqual(dv["dataverse_space"], "starlightsmovementz-masterbook")
            self.assertIn("demo.dataverse.org/dataverse/starlightsmovementz-masterbook", dv["space_url"])

    def test_list_institutions_cli(self) -> None:
        project_dir = Path(__file__).parent
        exit_code = main(
            ["--registry", str(project_dir / "endpoints_registry.json"), "--list-institutions"]
        )
        self.assertEqual(exit_code, 0)

    def test_export_masterbook_markdown(self) -> None:
        project_dir = Path(__file__).parent
        with tempfile.TemporaryDirectory() as tmp:
            md_path = Path(tmp) / "MASTERBOOK.md"
            exit_code = main(
                [
                    "--registry",
                    str(project_dir / "endpoints_registry.json"),
                    "--export-masterbook",
                    str(md_path),
                ]
            )
            self.assertEqual(exit_code, 0)
            content = md_path.read_text(encoding="utf-8")
            self.assertIn("# Institutional API-Masterbook (v2.4.0)", content)
            self.assertIn("Harvard Dataverse", content)
            self.assertIn("Library of Congress", content)

    def test_export_submissions_markdown(self) -> None:
        project_dir = Path(__file__).parent
        with tempfile.TemporaryDirectory() as tmp:
            sub_path = Path(tmp) / "SUBMISSION_ENDPOINTS.md"
            exit_code = main(
                [
                    "--registry",
                    str(project_dir / "endpoints_registry.json"),
                    "--extra-registry",
                    str(project_dir / "directory_registry.json"),
                    "--export-submissions",
                    str(sub_path),
                ]
            )
            self.assertEqual(exit_code, 0)
            content = sub_path.read_text(encoding="utf-8")
            self.assertIn("# Alle Einreichungs-Endpunkte", content)
            self.assertIn("POST /api/deposit/depositions", content)
            self.assertIn("submit_portal", content)

    def test_load_dotenv_if_present(self) -> None:
        import os

        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text('TEST_MASTERBOOK_KEY="secret123"\n# comment\n', encoding="utf-8")
            os.environ.pop("TEST_MASTERBOOK_KEY", None)
            load_dotenv_if_present(env_path)
            self.assertEqual(os.environ.get("TEST_MASTERBOOK_KEY"), "secret123")
            os.environ.pop("TEST_MASTERBOOK_KEY", None)

    def test_persist_verified_probes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            reg_path = Path(tmp) / "reg.json"
            reg_path.write_text(
                json.dumps({"masterbook_version": "2.3.0", "institutions": {"zenodo": {"name": "Zenodo"}}}),
                encoding="utf-8",
            )
            report = {
                "timestamp_utc": "2026-11-27T02:50:00Z",
                "results": [
                    {"key": "zenodo", "status": "OK", "http_status": 200, "url": "https://zenodo.org", "latency_ms": 42.0}
                ],
            }
            updated = persist_verified_probes(reg_path, report)
            self.assertEqual(updated, 1)
            saved = json.loads(reg_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["masterbook_version"], "2.4.0")
            self.assertEqual(saved["institutions"]["zenodo"]["verified_connection"]["status"], "OK")


if __name__ == "__main__":
    unittest.main()
