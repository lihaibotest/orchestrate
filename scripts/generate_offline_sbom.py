#!/usr/bin/env python3
from __future__ import annotations

from email.parser import BytesParser
import hashlib
import json
from pathlib import Path
import sys
import tarfile


REQUIRED = {"fastapi": "FastAPI", "pydantic": "Pydantic", "uvicorn": "Uvicorn"}


def layer_metadata(image_tar: Path) -> dict[str, tuple[str, str]]:
    packages: dict[str, tuple[str, str]] = {}
    with tarfile.open(image_tar) as archive:
        manifest_member = archive.extractfile("manifest.json")
        if manifest_member is None:
            raise SystemExit("docker save manifest.json is missing")
        manifest = json.load(manifest_member)
        if len(manifest) != 1:
            raise SystemExit("SBOM generator requires exactly one saved image")
        for layer_name in manifest[0].get("Layers", []):
            layer_member = archive.extractfile(layer_name)
            if layer_member is None:
                raise SystemExit(f"image layer is missing: {layer_name}")
            with tarfile.open(fileobj=layer_member) as layer:
                for member in layer.getmembers():
                    if not member.isfile() or not member.name.endswith(".dist-info/METADATA"):
                        continue
                    metadata_file = layer.extractfile(member)
                    if metadata_file is None:
                        continue
                    metadata = BytesParser().parsebytes(metadata_file.read())
                    name = str(metadata.get("Name", "")).strip()
                    version = str(metadata.get("Version", "")).strip()
                    if name and version:
                        packages[name.lower()] = (name, version)
    return packages


def spdx_id(name: str) -> str:
    normalized = "".join(character if character.isalnum() or character in ".-" else "-" for character in name)
    return f"SPDXRef-Package-{normalized}"


def main() -> None:
    if len(sys.argv) != 5:
        raise SystemExit("usage: generate_offline_sbom.py IMAGE_TAR OUTPUT IMAGE_ID BASE_IMAGE_ID")
    image_tar, output = Path(sys.argv[1]), Path(sys.argv[2])
    image_id, base_image_id = sys.argv[3:5]
    distributions = layer_metadata(image_tar)
    missing = sorted(key for key in REQUIRED if key not in distributions)
    if missing:
        raise SystemExit(f"runtime distributions missing from image SBOM: {missing}")

    namespace_hash = hashlib.sha256(f"{image_id}:orchestrator-agent:1.1.0".encode()).hexdigest()
    packages = [
        {
            "SPDXID": "SPDXRef-Package-orchestrator-agent",
            "name": "orchestrator-agent-standalone",
            "versionInfo": "1.1.0",
            "downloadLocation": "NOASSERTION",
            "filesAnalyzed": False,
            "supplier": "Organization: local-development",
            "externalRefs": [{"referenceCategory": "PACKAGE-MANAGER", "referenceType": "purl", "referenceLocator": "pkg:generic/orchestrator-agent-standalone@1.1.0"}],
        },
        {
            "SPDXID": "SPDXRef-Package-python-runtime",
            "name": "CPython",
            "versionInfo": "3.10",
            "downloadLocation": "NOASSERTION",
            "filesAnalyzed": False,
            "supplier": "Organization: Python Software Foundation",
        },
        {
            "SPDXID": "SPDXRef-Package-base-image",
            "name": "orchestrator-base",
            "versionInfo": base_image_id,
            "downloadLocation": "NOASSERTION",
            "filesAnalyzed": False,
            "supplier": "NOASSERTION",
        },
    ]
    for _key, (name, version) in sorted(distributions.items()):
        packages.append({
            "SPDXID": spdx_id(name), "name": name, "versionInfo": version,
            "downloadLocation": "NOASSERTION", "filesAnalyzed": False,
            "supplier": "NOASSERTION",
            "externalRefs": [{"referenceCategory": "PACKAGE-MANAGER", "referenceType": "purl", "referenceLocator": f"pkg:pypi/{name.lower()}@{version}"}],
        })
    relationships = [
        {"spdxElementId": "SPDXRef-DOCUMENT", "relationshipType": "DESCRIBES", "relatedSpdxElement": "SPDXRef-Package-orchestrator-agent"},
        *[
            {"spdxElementId": "SPDXRef-Package-orchestrator-agent", "relationshipType": "CONTAINS", "relatedSpdxElement": item["SPDXID"]}
            for item in packages[1:]
        ],
    ]
    document = {
        "spdxVersion": "SPDX-2.3", "dataLicense": "CC0-1.0", "SPDXID": "SPDXRef-DOCUMENT",
        "name": "orchestrator-agent-standalone-1.1.0",
        "documentNamespace": f"urn:uuid:{namespace_hash}",
        "creationInfo": {"created": "2026-08-18T00:00:00Z", "creators": ["Tool: bundled-offline-sbom-generator-1.0"]},
        "annotations": [{"annotationType": "OTHER", "annotator": "Tool: bundled-offline-sbom-generator-1.0", "annotationDate": "2026-08-18T00:00:00Z", "comment": f"image_id={image_id}"}],
        "packages": packages, "relationships": relationships,
    }
    output.write_text(json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
