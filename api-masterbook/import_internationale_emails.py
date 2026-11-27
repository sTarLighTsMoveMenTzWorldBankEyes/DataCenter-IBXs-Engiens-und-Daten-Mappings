#!/usr/bin/env python3
"""Import organizations, 162 catalog sources, and 53k+ domains from Internationale-Emails."""

from __future__ import annotations

import argparse
import csv
import json
import re
import urllib.parse
from pathlib import Path
from typing import Any


def slugify(text: str) -> str:
    """Create a deterministic lowercase registry key from an organization name or domain."""
    cleaned = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return cleaned[:48] or "org"


def normalize_base_url(raw_url: str, fallback_emails: str = "") -> str:
    """Extract scheme://netloc from a URL, or derive from email domain if URL is missing."""
    url = (raw_url or "").strip()
    if not url and fallback_emails:
        first_email = re.split(r"[;,]", fallback_emails)[0].strip()
        if "@" in first_email:
            url = f"https://{first_email.split('@', 1)[1].strip()}"
    if not url:
        return ""
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"
    parsed = urllib.parse.urlparse(url)
    if not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}".lower()


def build_entry(
    name: str,
    category: str,
    region: str,
    source: str,
    raw_url: str,
    emails: str,
    purpose: str = "",
) -> tuple[str, dict[str, Any]] | None:
    """Build a 5-stage-compatible registry entry for a directory organization."""
    base_url = normalize_base_url(raw_url, fallback_emails=emails)
    if not base_url or not name.strip():
        return None
    domain = urllib.parse.urlparse(base_url).netloc
    key = slugify(f"{name}_{domain}")
    email_list = [e.strip() for e in re.split(r"[;,]", emails or "") if e.strip()]
    mailto_target = f"mailto:{email_list[0]}" if email_list else base_url
    portal_url = raw_url.strip() or base_url
    entry: dict[str, Any] = {
        "name": name.strip(),
        "category": category.strip() or "Internationales Verzeichnis",
        "country": region.strip() or "Global",
        "source": source,
        "driver": "manual",
        "role": "submit_contact_and_verify",
        "enabled": False,
        "base_url": base_url,
        "docs_url": portal_url,
        "auth": {"type": "contact_or_portal"},
        "contact_emails": email_list[:5],
        "endpoints": {
            "submit_portal": portal_url,
            "submit_contact": mailto_target,
            "validate_openapi": f"GET {base_url}/.well-known/openapi.json",
            "validate_swagger": f"GET {base_url}/swagger.json",
            "validate_sitemap": f"GET {base_url}/sitemap.xml",
            "verify_health": f"HEAD {base_url}/",
            "proof_receipt": "HTTP-Status + SHA-256 + Kontakt-Quittung",
        },
    }
    if purpose.strip():
        entry["purpose"] = purpose.strip()
    return key, entry


def import_directory(
    data_dir: Path,
    output_path: Path,
    domains_output_path: Path | None = None,
) -> tuple[int, int]:
    """Read all CSV files from Internationale-Emails/data into registry JSON + optional domain index."""
    institutions: dict[str, Any] = {}
    seen_domains: set[str] = set()

    def add_record(result: tuple[str, dict[str, Any]] | None) -> None:
        if result is None:
            return
        key, entry = result
        domain = urllib.parse.urlparse(entry["base_url"]).netloc
        if domain in seen_domains:
            return
        seen_domains.add(domain)
        while key in institutions:
            key = f"{key}_2"
        institutions[key] = entry

    master_csv = data_dir / "master_emails.csv"
    if master_csv.is_file():
        with master_csv.open("r", encoding="utf-8", errors="replace") as handle:
            for row in csv.DictReader(handle):
                emails = row.get("emails_validated") or row.get("emails") or ""
                add_record(
                    build_entry(
                        name=row.get("name", ""),
                        category=row.get("kategorie", "") or row.get("quelle", "master_emails"),
                        region=row.get("region", ""),
                        source=row.get("quelle", "master_emails"),
                        raw_url=row.get("website", ""),
                        emails=emails,
                    )
                )

    for csv_name, name_col, source_label in (
        ("finance_foerder_banken_2026-11-09.csv", "Institution", "finance_foerder_banken"),
        ("finance_foerder_portale_2026-11-09.csv", "Portal/Netzwerk", "finance_foerder_portale"),
    ):
        csv_path = data_dir / csv_name
        if csv_path.is_file():
            with csv_path.open("r", encoding="utf-8", errors="replace") as handle:
                for row in csv.DictReader(handle, delimiter=";"):
                    add_record(
                        build_entry(
                            name=row.get(name_col, ""),
                            category=row.get("Kategorie", "Finanzen & Förderportale"),
                            region="Global / EU",
                            source=source_label,
                            raw_url=row.get("Quell-URL", ""),
                            emails=row.get("E-Mail", ""),
                            purpose=row.get("Kontaktzweck", ""),
                        )
                    )

    # Also aggregate all 162 official sources & 53k+ domains from email_katalog_global
    catalog_csv = data_dir / "email_katalog_global_2026-11-09.csv"
    source_stats: dict[str, dict[str, Any]] = {}
    domain_map: dict[str, dict[str, Any]] = {}

    if catalog_csv.is_file():
        with catalog_csv.open("r", encoding="utf-8", errors="replace") as handle:
            reader = csv.reader(handle, delimiter=";")
            next(reader, None)
            for row in reader:
                if len(row) < 2:
                    continue
                src, email = row[0].strip(), row[1].strip()
                if not src or "@" not in email:
                    continue
                domain = email.split("@", 1)[1].lower()
                st = source_stats.setdefault(src, {"count": 0, "domains": set(), "sample_email": email})
                st["count"] += 1
                st["domains"].add(domain)

                if domain not in domain_map:
                    domain_map[domain] = {
                        "domain": domain,
                        "base_url": f"https://{domain}",
                        "source": src,
                        "sample_emails": [email],
                        "endpoints": {
                            "submit_contact": f"mailto:{email}",
                            "validate_openapi": f"GET https://{domain}/.well-known/openapi.json",
                            "validate_sitemap": f"GET https://{domain}/sitemap.xml",
                            "verify_health": f"HEAD https://{domain}/",
                        },
                    }
                elif len(domain_map[domain]["sample_emails"]) < 3 and email not in domain_map[domain]["sample_emails"]:
                    domain_map[domain]["sample_emails"].append(email)

        for src, st in sorted(source_stats.items()):
            first_domain = sorted(st["domains"])[0]
            key = slugify(f"katalog_{src}")
            while key in institutions:
                key = f"{key}_2"
            institutions[key] = {
                "name": f"{src} ({st['count']} Kontakte / {len(st['domains'])} Domains)",
                "category": "Globaler Behörden- & Katalog-Index (162 Quellen)",
                "country": src.split("–")[0].strip() if "–" in src else "Global",
                "source": "email_katalog_global",
                "driver": "manual",
                "role": "submit_contact_and_verify",
                "enabled": False,
                "base_url": f"https://{first_domain}",
                "docs_url": f"https://{first_domain}",
                "auth": {"type": "contact_or_portal"},
                "contact_emails": [st["sample_email"]],
                "endpoints": {
                    "submit_contact": f"mailto:{st['sample_email']}",
                    "validate_openapi": f"GET https://{first_domain}/.well-known/openapi.json",
                    "validate_sitemap": f"GET https://{first_domain}/sitemap.xml",
                    "verify_health": f"HEAD https://{first_domain}/",
                    "proof_receipt": "HTTP-Status + SHA-256 + Kontakt-Quittung",
                },
            }

    payload = {
        "masterbook_version": "2.4.0-directory",
        "schema": "5-stage-institutional-registry",
        "source_repository": "https://github.com/EU-UNION-AI-PACT/Internationale-Emails",
        "institutions": institutions,
    }
    output_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    if domains_output_path is not None and domain_map:
        with domains_output_path.open("w", encoding="utf-8") as handle:
            for item in domain_map.values():
                handle.write(json.dumps(item, ensure_ascii=False) + "\n")

    return len(institutions), len(domain_map)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Import Internationale-Emails CSVs into a Masterbook registry JSON."
    )
    parser.add_argument("--data-dir", type=Path, required=True, help="Path to Internationale-Emails/data")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("directory_registry.json"),
        help="Output registry JSON path (default: directory_registry.json)",
    )
    parser.add_argument(
        "--domains-jsonl",
        type=Path,
        default=Path("global_domains_index.jsonl"),
        help="Output JSONL path for all unique email domains (default: global_domains_index.jsonl)",
    )
    args = parser.parse_args()
    inst_count, domain_count = import_directory(args.data_dir, args.output, args.domains_jsonl)
    print(f"Imported {inst_count} institutions into {args.output} and {domain_count} unique domains into {args.domains_jsonl}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
