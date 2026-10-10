#!/usr/bin/env python3
import argparse
import fnmatch
import hashlib
import json
import shutil
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "build" / "source-allowlists.json"
DEFAULT_OUTPUT = ROOT / "build" / "contexts"


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_denied(relative, patterns):
    value = relative.as_posix()
    return any(
        fnmatch.fnmatch(value, pattern)
        or fnmatch.fnmatch(relative.name, pattern)
        or pattern in relative.parts
        for pattern in patterns
    )


def selected_files(source_root, include_patterns, deny_patterns):
    selected = set()
    for pattern in include_patterns:
        for path in source_root.glob(pattern):
            if path.is_file():
                relative = path.relative_to(source_root)
                if not is_denied(relative, deny_patterns):
                    selected.add(relative)
    if not selected:
        raise RuntimeError(f"allowlist selected no files from {source_root}")
    return sorted(selected, key=lambda path: path.as_posix())


def prepare_contexts(config_path=DEFAULT_CONFIG, output_root=DEFAULT_OUTPUT):
    config_path = Path(config_path).resolve()
    output_root = Path(output_root).resolve()
    config = json.loads(config_path.read_text(encoding="utf-8"))
    build_root = config_path.parent
    output_root.mkdir(parents=True, exist_ok=True)
    manifest = {"schema_version": 1, "components": {}}

    for name, component in config["components"].items():
        source_root = Path(component["source_root"]).resolve()
        context = output_root / name
        if context.exists():
            shutil.rmtree(context)
        (context / "source").mkdir(parents=True)
        files = selected_files(source_root, component["include"], config["deny"])
        hashes = {}
        for relative in files:
            source = source_root / relative
            destination = context / "source" / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
            hashes[relative.as_posix()] = sha256_file(source)

        shutil.copyfile(build_root / name / "Dockerfile", context / "Dockerfile")
        release_root = build_root / name / "release"
        if release_root.is_dir():
            shutil.copytree(release_root, context / "release")
        manifest["components"][name] = {
            "source_root": str(source_root),
            "files": hashes,
        }

    manifest_path = output_root / "selected-source-manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description="Prepare allowlisted Agent build contexts")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    args = parser.parse_args(argv)
    prepare_contexts(args.config, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
