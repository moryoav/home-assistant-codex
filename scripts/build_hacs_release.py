"""Check a release tag and build a reproducible HACS integration ZIP."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TAG_PATTERN = re.compile(r"v\d+\.\d+\.\d+")
DOMAIN = "codex_cli"
ASSET_NAME = "codex_cli.zip"
CHANGELOG = ROOT / "codex-cli-worker" / "CHANGELOG.md"


def build(tag: str, output_dir: Path) -> Path:
    """Validate release metadata and write the integration ZIP and release notes."""
    if TAG_PATTERN.fullmatch(tag) is None:
        raise ValueError(f"Expected a vX.Y.Z tag, got {tag!r}")

    version = tag[1:]
    component_dir = ROOT / "custom_components" / DOMAIN
    manifest = json.loads((component_dir / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("domain") != DOMAIN:
        raise ValueError(f"Integration domain must be {DOMAIN}")
    if manifest.get("version") != version:
        raise ValueError(f"Manifest version {manifest.get('version')!r} does not match {tag}")

    worker_config = (ROOT / "codex-cli-worker" / "config.yaml").read_text(encoding="utf-8")
    worker_version = re.search(r'^version:\s*"?(\d+\.\d+\.\d+)"?\s*$', worker_config, re.MULTILINE)
    if worker_version is None or worker_version.group(1) != version:
        raise ValueError(f"Worker config version does not match {tag}")

    lines = CHANGELOG.read_text(encoding="utf-8").splitlines()
    heading = re.compile(rf"^## {re.escape(version)}(?:\s+-\s+.*)?$")
    start = next((index + 1 for index, line in enumerate(lines) if heading.fullmatch(line)), None)
    if start is None:
        raise ValueError(f"{CHANGELOG.relative_to(ROOT)} has no heading for {tag}")
    end = next((index for index in range(start, len(lines)) if lines[index].startswith("## ")), len(lines))
    notes = "\n".join(lines[start:end]).strip()
    if not notes:
        raise ValueError(f"{CHANGELOG.relative_to(ROOT)} has no notes for {tag}")

    hacs = json.loads((ROOT / "hacs.json").read_text(encoding="utf-8"))
    if hacs.get("zip_release") is not True or hacs.get("filename") != ASSET_NAME:
        raise ValueError(f"hacs.json must select {ASSET_NAME} as its ZIP release")
    if hacs.get("hide_default_branch") is not True:
        raise ValueError("hacs.json must hide the default branch for ZIP releases")

    source_tree = f"HEAD:custom_components/{DOMAIN}"
    tree = subprocess.run(
        ["git", "ls-tree", "-r", "-z", "--name-only", source_tree],
        cwd=ROOT,
        check=True,
        stdout=subprocess.PIPE,
    )
    names = sorted(name.decode("utf-8") for name in tree.stdout.split(b"\0") if name)
    if not names:
        raise ValueError("Tagged integration tree is empty")

    output_dir.mkdir(parents=True, exist_ok=True)
    archive_path = output_dir / ASSET_NAME
    with zipfile.ZipFile(archive_path, mode="w") as archive:
        for name in names:
            contents = subprocess.run(
                ["git", "show", f"{source_tree}/{name}"],
                cwd=ROOT,
                check=True,
                stdout=subprocess.PIPE,
            ).stdout
            entry = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            entry.create_system = 3
            entry.external_attr = 0o100644 << 16
            entry.compress_type = zipfile.ZIP_STORED
            archive.writestr(entry, contents)

    with zipfile.ZipFile(archive_path) as archive:
        names = set(archive.namelist())
        if not {"__init__.py", "manifest.json"}.issubset(names):
            raise ValueError("ZIP is missing integration files at its root")
        if archive.testzip() is not None:
            raise ValueError("ZIP integrity check failed")
        archived_manifest = json.loads(archive.read("manifest.json"))
        if archived_manifest != manifest:
            raise ValueError("Tagged manifest differs from the working tree")

    (output_dir / "release_notes.md").write_text(notes + "\n", encoding="utf-8")
    return archive_path


def main() -> int:
    """Build the requested release and return a command-line exit status."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag", help="vX.Y.Z release tag")
    parser.add_argument("output_dir", type=Path, help="Directory outside the repository for the ZIP")
    args = parser.parse_args()
    try:
        print(build(args.tag, args.output_dir))
    except (OSError, ValueError, subprocess.CalledProcessError, zipfile.BadZipFile) as exc:
        print(f"HACS release build failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
