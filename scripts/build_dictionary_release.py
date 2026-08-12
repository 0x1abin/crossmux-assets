#!/usr/bin/env python3
"""Validate StarDict resources and stage a dictionary release."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
import shutil
import struct
import sys
import zlib
from pathlib import Path
from urllib.parse import urlparse

MANIFEST_VERSION = 1
MAX_DICTIONARIES = 64
MAX_FILE_BYTES = 100_000_000
MAX_REVISION = 0x7FFFFFFF
MAX_HEADWORD_BYTES = 255
MAX_DEFINITION_BYTES = 64 * 1024
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
LANGUAGE_RE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")
ASCII_LOWER = bytes.maketrans(b"ABCDEFGHIJKLMNOPQRSTUVWXYZ", b"abcdefghijklmnopqrstuvwxyz")


class BuildError(ValueError):
    pass


def valid_url(value: object) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def crc32(path: Path) -> int:
    checksum = 0
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(64 * 1024), b""):
            checksum = zlib.crc32(chunk, checksum)
    return checksum & 0xFFFFFFFF


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def dictionary_data_size(path: Path) -> int:
    if not path.name.endswith(".dict.dz"):
        return path.stat().st_size
    size = 0
    try:
        with gzip.open(path, "rb") as stream:
            for chunk in iter(lambda: stream.read(64 * 1024), b""):
                size += len(chunk)
                if size > 0xFFFFFFFF:
                    raise BuildError(f"{path.name}: uncompressed dictionary exceeds 32-bit offsets")
    except OSError as exc:
        raise BuildError(f"{path.name}: invalid dictzip stream: {exc}") from exc
    return size


def validate_index(dictionary_id: str, path: Path, data_size: int) -> None:
    previous = None
    entries = 0
    with path.open("rb") as stream:
        while True:
            headword = bytearray()
            while True:
                value = stream.read(1)
                if not value:
                    if not headword:
                        if entries == 0:
                            raise BuildError(f"{dictionary_id}: empty .idx")
                        return
                    raise BuildError(f"{dictionary_id}: truncated .idx headword")
                if value == b"\0":
                    break
                headword.extend(value)
                if len(headword) > MAX_HEADWORD_BYTES:
                    raise BuildError(f"{dictionary_id}: .idx headword exceeds {MAX_HEADWORD_BYTES} bytes")
            if not headword:
                raise BuildError(f"{dictionary_id}: empty .idx headword")
            try:
                headword.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise BuildError(f"{dictionary_id}: .idx headword is not UTF-8") from exc
            suffix = stream.read(8)
            if len(suffix) != 8:
                raise BuildError(f"{dictionary_id}: truncated .idx record")
            offset, size = struct.unpack(">II", suffix)
            if size == 0 or size > MAX_DEFINITION_BYTES or offset > data_size or size > data_size - offset:
                raise BuildError(f"{dictionary_id}: .idx definition range is invalid")
            key = bytes(headword).translate(ASCII_LOWER)
            if previous is not None and key <= previous:
                raise BuildError(f"{dictionary_id}: .idx is not strictly ASCII-case-insensitive sorted")
            previous = key
            entries += 1


def load_catalog(path: Path) -> dict:
    try:
        catalog = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BuildError(f"cannot read catalog {path}: {exc}") from exc

    if not isinstance(catalog, dict) or catalog.get("version") != MANIFEST_VERSION:
        raise BuildError(f"catalog version must be {MANIFEST_VERSION}")
    revision = catalog.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or not 1 <= revision <= MAX_REVISION:
        raise BuildError(f"catalog revision must be an integer from 1 to {MAX_REVISION}")
    dictionaries = catalog.get("dictionaries")
    if not isinstance(dictionaries, list) or len(dictionaries) > MAX_DICTIONARIES:
        raise BuildError(f"catalog dictionaries must be a list of at most {MAX_DICTIONARIES} items")

    names: set[str] = set()
    for entry in dictionaries:
        if not isinstance(entry, dict):
            raise BuildError("each dictionary must be an object")
        dictionary_id = entry.get("id")
        if (
            not isinstance(dictionary_id, str)
            or len(dictionary_id.encode("ascii", errors="ignore")) != len(dictionary_id)
            or len(dictionary_id) > 31
            or not ID_RE.fullmatch(dictionary_id)
        ):
            raise BuildError(f"invalid dictionary id: {dictionary_id!r}")
        if dictionary_id in names:
            raise BuildError(f"duplicate dictionary id: {dictionary_id}")
        names.add(dictionary_id)

        for key, limit in (("name", 64), ("description", 160)):
            value = entry.get(key)
            if not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > limit:
                raise BuildError(f"{dictionary_id}: {key} must be 1-{limit} UTF-8 bytes")
        item_revision = entry.get("revision")
        if (
            not isinstance(item_revision, int)
            or isinstance(item_revision, bool)
            or not 1 <= item_revision <= MAX_REVISION
        ):
            raise BuildError(f"{dictionary_id}: revision must be an integer from 1 to {MAX_REVISION}")
        languages = entry.get("languages")
        if (
            not isinstance(languages, list)
            or not 1 <= len(languages) <= 4
            or not all(isinstance(lang, str) and LANGUAGE_RE.fullmatch(lang) for lang in languages)
        ):
            raise BuildError(f"{dictionary_id}: languages must contain 1-4 BCP-47 codes")
        license_info = entry.get("license")
        if (
            not isinstance(license_info, dict)
            or not isinstance(license_info.get("name"), str)
            or not license_info["name"].strip()
            or not valid_url(license_info.get("url"))
        ):
            raise BuildError(f"{dictionary_id}: redistribution license name and URL are required")
        if not valid_url(entry.get("sourceUrl")):
            raise BuildError(f"{dictionary_id}: sourceUrl must be HTTP(S)")
        if "files" in entry:
            raise BuildError(f"{dictionary_id}: files are generated, not catalog input")
    return catalog


def dictionary_files(root: Path, dictionary_id: str) -> list[Path]:
    directory = root / "dictionaries" / dictionary_id
    if not directory.is_dir():
        raise BuildError(f"missing dictionary directory: dictionaries/{dictionary_id}")

    idx = directory / f"{dictionary_id}.idx"
    plain = directory / f"{dictionary_id}.dict"
    compressed = directory / f"{dictionary_id}.dict.dz"
    ifo = directory / f"{dictionary_id}.ifo"
    if not idx.is_file():
        raise BuildError(f"{dictionary_id}: missing uncompressed .idx")
    if plain.is_file() == compressed.is_file():
        raise BuildError(f"{dictionary_id}: provide exactly one .dict or .dict.dz")

    expected = {idx, plain if plain.is_file() else compressed}
    if ifo.is_file():
        expected.add(ifo)

    actual = set(directory.iterdir())
    if actual != expected:
        unexpected = ", ".join(sorted(path.name for path in actual - expected))
        missing = ", ".join(sorted(path.name for path in expected - actual))
        raise BuildError(f"{dictionary_id}: unexpected files [{unexpected}], missing files [{missing}]")
    if any(path.is_symlink() or not path.is_file() for path in expected):
        raise BuildError(f"{dictionary_id}: dictionary resources must be regular files")

    files = sorted(expected, key=lambda path: path.name)
    for path in files:
        size = path.stat().st_size
        if size == 0 or size >= MAX_FILE_BYTES:
            raise BuildError(f"{dictionary_id}: {path.name} must be 1-{MAX_FILE_BYTES - 1} bytes")
    if ifo.is_file():
        metadata = {}
        try:
            with ifo.open("r", encoding="utf-8", errors="strict") as stream:
                for line in stream:
                    key, separator, value = line.rstrip("\r\n").partition("=")
                    if separator:
                        metadata[key.strip().lower()] = value.strip()
        except UnicodeError as exc:
            raise BuildError(f"{dictionary_id}: .ifo is not UTF-8") from exc
        if metadata.get("idxoffsetbits") == "64":
            raise BuildError(f"{dictionary_id}: 64-bit index offsets are unsupported")
        if metadata.get("sametypesequence") != "m":
            raise BuildError(f"{dictionary_id}: .ifo must declare sametypesequence=m")
    data_file = plain if plain.is_file() else compressed
    validate_index(dictionary_id, idx, dictionary_data_size(data_file))
    return files


def build_manifest(root: Path, catalog_path: Path) -> tuple[dict, list[Path]]:
    catalog = load_catalog(catalog_path)
    catalog_ids = {entry["id"] for entry in catalog["dictionaries"]}
    dictionaries_root = root / "dictionaries"
    directory_ids = (
        {path.name for path in dictionaries_root.iterdir() if path.is_dir() and not path.name.startswith(".")}
        if dictionaries_root.is_dir()
        else set()
    )
    if directory_ids != catalog_ids:
        missing = ", ".join(sorted(catalog_ids - directory_ids))
        unexpected = ", ".join(sorted(directory_ids - catalog_ids))
        raise BuildError(f"catalog/resource mismatch: missing [{missing}], unexpected [{unexpected}]")

    manifest_items = []
    release_files: list[Path] = []
    for entry in sorted(catalog["dictionaries"], key=lambda item: item["id"]):
        files = dictionary_files(root, entry["id"])
        output = dict(entry)
        output["files"] = [
            {"name": path.name, "size": path.stat().st_size, "crc32": crc32(path)} for path in files
        ]
        manifest_items.append(output)
        release_files.extend(files)

    return {
        "version": MANIFEST_VERSION,
        "revision": catalog["revision"],
        "dictionaries": manifest_items,
    }, release_files


def stage_release(root: Path, catalog_path: Path, output_dir: Path) -> dict:
    manifest, release_files = build_manifest(root, catalog_path)
    if output_dir.exists() and any(output_dir.iterdir()):
        raise BuildError(f"output directory is not empty: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    for source in release_files:
        shutil.copy2(source, output_dir / source.name)
    manifest_path = output_dir / "dictionaries.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    staged = sorted(output_dir.iterdir(), key=lambda path: path.name)
    sums = "".join(f"{sha256(path)}  {path.name}\n" for path in staged)
    (output_dir / "SHA256SUMS").write_text(sums, encoding="ascii")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--output-dir", type=Path)
    args = parser.parse_args()

    root = Path(__file__).resolve().parent.parent
    try:
        if args.check:
            manifest, _ = build_manifest(root, root / "catalogs" / "dictionaries.json")
        else:
            manifest = stage_release(root, root / "catalogs" / "dictionaries.json", args.output_dir.resolve())
    except BuildError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
