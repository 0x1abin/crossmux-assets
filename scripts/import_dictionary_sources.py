#!/usr/bin/env python3
"""Build the reviewed CrossMux StarDict resources from pinned upstream archives."""

from __future__ import annotations

import argparse
import filecmp
import gzip
import hashlib
import re
import shutil
import struct
import tarfile
import tempfile
import urllib.request
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path

MAX_HEADWORD_BYTES = 255
MAX_DEFINITION_BYTES = 64 * 1024


@dataclass(frozen=True)
class Source:
    dictionary_id: str
    filename: str
    url: str
    sha256: str


SOURCES = (
    Source(
        dictionary_id="oewn-en-en",
        filename="english-wordnet-2025.zip",
        url="https://en-word.net/static/english-wordnet-2025.zip",
        sha256="38b16326159f51853626b7d24a44c453fa88ab33f06fce5ec8fc5996d1c2be93",
    ),
    Source(
        dictionary_id="freedict-en-zh",
        filename="freedict-eng-zho-2025.11.23.stardict.tar.xz",
        url=(
            "https://download.freedict.org/dictionaries/eng-zho/2025.11.23/"
            "freedict-eng-zho-2025.11.23.stardict.tar.xz"
        ),
        sha256="9dbae6bb5558906cc05f1e573bee2deab8b6e09adfb16fc496288926882435af",
    ),
)

POS_NAMES = {"n": "noun", "v": "verb", "a": "adjective", "s": "adjective", "r": "adverb"}
ASCII_LOWER = bytes.maketrans(b"ABCDEFGHIJKLMNOPQRSTUVWXYZ", b"abcdefghijklmnopqrstuvwxyz")


class ImportError(ValueError):
    pass


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ascii_case_key(word: str) -> tuple[bytes, bytes]:
    encoded = word.encode("utf-8")
    return encoded.translate(ASCII_LOWER), encoded


def normalize_headword(word: str) -> str:
    encoded = word.replace("_", " ").strip().encode("utf-8")
    return encoded.translate(ASCII_LOWER).decode("utf-8")


def append_definition(entries: dict[str, list[str]], headword: str, definition: str) -> None:
    headword = normalize_headword(headword)
    definition = definition.strip()
    encoded = headword.encode("utf-8")
    if not headword or len(encoded) > MAX_HEADWORD_BYTES:
        raise ImportError(f"invalid headword length: {headword!r}")
    if not definition:
        raise ImportError(f"empty definition for {headword!r}")
    if definition not in entries[headword]:
        entries[headword].append(definition)


class PlainTextParser(HTMLParser):
    BLOCK_START = {"div", "p", "ol", "ul"}
    BLOCK_END = BLOCK_START | {"li"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def newline(self) -> None:
        if self.parts and self.parts[-1] != "\n":
            self.parts.append("\n")

    def handle_starttag(self, tag: str, attrs) -> None:
        del attrs
        if tag in self.BLOCK_START or tag == "br":
            self.newline()
        elif tag == "li":
            self.newline()
            self.parts.append("- ")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.BLOCK_END:
            self.newline()

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def text(self) -> str:
        lines = []
        for raw in "".join(self.parts).splitlines():
            line = " ".join(raw.split())
            if line and line != "-":
                lines.append(line)
        return "\n".join(lines)


def html_to_text(value: str) -> str:
    parser = PlainTextParser()
    parser.feed(value)
    parser.close()
    return parser.text()


def parse_stardict_index(data: bytes) -> list[tuple[str, int, int]]:
    records = []
    cursor = 0
    while cursor < len(data):
        end = data.find(b"\0", cursor)
        if end < 0 or end == cursor or end + 9 > len(data):
            raise ImportError("invalid upstream StarDict index")
        try:
            word = data[cursor:end].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ImportError("upstream StarDict headword is not UTF-8") from exc
        offset, size = struct.unpack(">II", data[end + 1 : end + 9])
        records.append((word, offset, size))
        cursor = end + 9
    return records


def import_freedict(archive: Path) -> dict[str, list[str]]:
    entries: dict[str, list[str]] = defaultdict(list)
    with tarfile.open(archive, "r:xz") as bundle:
        try:
            compressed_index = bundle.extractfile("eng-zho/eng-zho.idx.gz")
            dictionary = bundle.extractfile("eng-zho/eng-zho.dict")
        except KeyError as exc:
            raise ImportError(f"missing FreeDict archive member: {exc}") from exc
        if compressed_index is None or dictionary is None:
            raise ImportError("FreeDict archive contains a non-file member")
        index_data = gzip.decompress(compressed_index.read())
        dictionary_data = dictionary.read()

    for headword, offset, size in parse_stardict_index(index_data):
        if size == 0 or offset + size > len(dictionary_data):
            raise ImportError(f"FreeDict definition is out of bounds: {headword!r}")
        try:
            definition = dictionary_data[offset : offset + size].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ImportError(f"FreeDict definition is not UTF-8: {headword!r}") from exc
        append_definition(entries, headword, html_to_text(definition))
    return entries


def parse_wordnet_data(data: str, entries: dict[str, list[str]]) -> None:
    for line in data.splitlines():
        if not re.match(r"^\d{8} ", line):
            continue
        fields, separator, gloss = line.partition(" | ")
        if not separator:
            raise ImportError("invalid WordNet data line")
        tokens = fields.split()
        try:
            pos = tokens[2]
            word_count = int(tokens[3], 16)
            words = [normalize_headword(tokens[4 + index * 2]) for index in range(word_count)]
        except (IndexError, ValueError) as exc:
            raise ImportError("invalid WordNet synset fields") from exc
        synonyms = ", ".join(words)
        definition = f"({POS_NAMES.get(pos, pos)}) {gloss.strip()}"
        if len(words) > 1:
            definition += f"\nSynonyms: {synonyms}"
        for word in words:
            append_definition(entries, word, definition)


def parse_wordnet_exceptions(data: str, entries: dict[str, list[str]], pos: str | None = None) -> None:
    prefix = f"({POS_NAMES[pos]})" if pos else None
    for line in data.splitlines():
        forms = [normalize_headword(value) for value in line.split()]
        if len(forms) < 2:
            continue
        definitions = []
        for lemma in forms[1:]:
            for definition in entries.get(lemma, ()):
                if (prefix is None or definition.startswith(prefix)) and definition not in definitions:
                    definitions.append(definition)
        if definitions:
            entries[forms[0]] = definitions + [
                definition for definition in entries.get(forms[0], ()) if definition not in definitions
            ]


def import_wordnet(archive: Path) -> dict[str, list[str]]:
    entries: dict[str, list[str]] = defaultdict(list)
    with zipfile.ZipFile(archive) as bundle:
        for suffix in ("noun", "verb", "adj", "adv"):
            try:
                data = bundle.read(f"oewn2025/data.{suffix}").decode("utf-8")
            except KeyError as exc:
                raise ImportError(f"missing WordNet archive member: {exc}") from exc
            parse_wordnet_data(data, entries)
        for suffix in ("noun", "verb", "adj", "adv"):
            try:
                data = bundle.read(f"oewn2025/{suffix}.exc").decode("utf-8")
            except KeyError as exc:
                raise ImportError(f"missing WordNet exception member: {exc}") from exc
            pos = {"noun": "n", "verb": "v", "adj": "a", "adv": "r"}[suffix]
            parse_wordnet_exceptions(data, entries, pos)
    return entries


def write_stardict(
    output_root: Path,
    dictionary_id: str,
    entries: dict[str, list[str]],
    *,
    name: str,
    date: str,
    description: str,
) -> None:
    directory = output_root / dictionary_id
    directory.mkdir(parents=True, exist_ok=True)
    index = bytearray()
    dictionary = bytearray()
    for word in sorted(entries, key=ascii_case_key):
        definition = "\n\n".join(entries[word]).encode("utf-8")
        if not 0 < len(definition) <= MAX_DEFINITION_BYTES:
            raise ImportError(f"definition for {word!r} is larger than {MAX_DEFINITION_BYTES} bytes")
        word_bytes = word.encode("utf-8")
        index.extend(word_bytes)
        index.append(0)
        index.extend(struct.pack(">II", len(dictionary), len(definition)))
        dictionary.extend(definition)

    (directory / f"{dictionary_id}.idx").write_bytes(index)
    (directory / f"{dictionary_id}.dict").write_bytes(dictionary)
    ifo = (
        "StarDict's dict ifo file\n"
        "version=3.0.0\n"
        f"bookname={name}\n"
        f"wordcount={len(entries)}\n"
        f"idxfilesize={len(index)}\n"
        "sametypesequence=m\n"
        f"date={date}\n"
        f"description={description}\n"
    )
    (directory / f"{dictionary_id}.ifo").write_text(ifo, encoding="utf-8")


def verify_source(source: Source, path: Path) -> None:
    if not path.is_file():
        raise ImportError(f"missing source archive: {path}")
    actual = sha256(path)
    if actual != source.sha256:
        raise ImportError(f"checksum mismatch for {source.filename}: {actual}")


def download_sources(directory: Path) -> None:
    for source in SOURCES:
        destination = directory / source.filename
        with urllib.request.urlopen(source.url) as response, destination.open("wb") as output:
            shutil.copyfileobj(response, output, 64 * 1024)


def build_resources(source_dir: Path, output_root: Path) -> None:
    sources = {source.dictionary_id: source_dir / source.filename for source in SOURCES}
    for source in SOURCES:
        verify_source(source, sources[source.dictionary_id])
    write_stardict(
        output_root,
        "oewn-en-en",
        import_wordnet(sources["oewn-en-en"]),
        name="Open English WordNet 2025",
        date="2025-12-31",
        description=(
            "Open English WordNet 2025, CC BY 4.0, converted to plain-text StarDict by CrossMux; "
            "source: https://en-word.net/downloads"
        ),
    )
    write_stardict(
        output_root,
        "freedict-en-zh",
        import_freedict(sources["freedict-en-zh"]),
        name="FreeDict English-Chinese 2025.11.23",
        date="2025-11-23",
        description=(
            "FreeDict and WikDict English-Chinese 2025.11.23, CC BY-SA 3.0, converted to plain-text "
            "StarDict by CrossMux; source: https://freedict.org/downloads/"
        ),
    )


def compare_directories(expected: Path, actual: Path) -> None:
    comparison = filecmp.dircmp(expected, actual)
    if comparison.left_only or comparison.right_only or comparison.funny_files:
        raise ImportError("committed dictionary file set differs from regenerated resources")
    _, mismatch, errors = filecmp.cmpfiles(expected, actual, comparison.common_files, shallow=False)
    if mismatch or errors:
        raise ImportError(f"committed dictionary content differs: {', '.join(mismatch + errors)}")
    for directory in comparison.common_dirs:
        compare_directories(expected / directory, actual / directory)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, help="directory containing the pinned source archives")
    parser.add_argument("--output-root", type=Path, help="dictionary output root (defaults to dictionaries/)")
    parser.add_argument("--check", action="store_true", help="regenerate and compare with committed resources")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent

    try:
        with tempfile.TemporaryDirectory() as temporary:
            temporary_path = Path(temporary)
            source_dir = args.source_dir.resolve() if args.source_dir else temporary_path / "sources"
            if args.source_dir is None:
                source_dir.mkdir()
                download_sources(source_dir)
            if args.check:
                generated = temporary_path / "dictionaries"
                build_resources(source_dir, generated)
                compare_directories(root / "dictionaries", generated)
            else:
                output_root = args.output_root.resolve() if args.output_root else root / "dictionaries"
                build_resources(source_dir, output_root)
    except (ImportError, OSError, tarfile.TarError, zipfile.BadZipFile) as exc:
        print(f"ERROR: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
