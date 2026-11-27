# Institutional API-Masterbook

Zentrale Orchestrierungs-Architektur & Registry für Multi-Repository-Publikationen (Harvard Dataverse, Zenodo/CERN, OSF, Software Heritage, DNB, Arweave) inklusive kryptografischem Nachweis-Zertifikat (`publication_certificate.json`).

## Dateien

- `endpoints_registry.json`: Maschinenlesbare 5-Stufen-Registry (Auth, Create Space, Upload & Publish, Proof Receipt, Verification Endpoint).
- `institutional_masterbook.py`: Ausführbarer Python-3-Client (keine externen Abhängigkeiten nötig, reine Standardbibliothek).
- `package_metadata.example.json`: Beispiel-Metadaten für dein Projekt/Artefakt.
- `test_masterbook.py`: Unit-Tests für Hashing, Dry-Run und Zertifikats-Generierung.

## Schnellstart (Dry-Run ohne API-Keys)

Alle registrierten Institutionen, Rollen und benötigten Umgebungsvariablen anzeigen:

```bash
python3 institutional_masterbook.py --list-institutions
```

Dry-Run für alle aktivierten Publish-/Archiv-Plattformen (Harvard Dataverse, Zenodo, OSF, Figshare, Dryad, Internet Archive, Software Heritage):

```bash
python3 institutional_masterbook.py \
  --metadata package_metadata.example.json \
  --sandbox \
  --dry-run
```

Erzeugt `publication_certificate.json` mit dem lokalen SHA-256-Hash und simulierten Quittungen für alle aktivierten Institutionen.

## Produktiv- / Sandbox-Nutzung

1. `package_metadata.example.json` kopieren (z. B. nach `package_metadata.json`) und anpassen (`project_title`, `author.orcid`, `artifact_files`, `git_repository_url`).
2. API-Tokens als Umgebungsvariablen setzen (nur für die gewünschten Dienste):
   ```bash
   export ZENODO_API_TOKEN="..."
   export DATAVERSE_API_TOKEN="..."
   export OSF_API_TOKEN="..."
   export SWH_API_TOKEN="..."   # optional für öffentliche Git-Repos
   ```
3. Erst gegen die Sandbox-Umgebungen testen (`--sandbox`):
   ```bash
   python3 institutional_masterbook.py \
     --metadata package_metadata.json \
     --institutions zenodo software_heritage \
     --sandbox
   ```
4. Für den echten Live-Publish `--sandbox` weglassen.
