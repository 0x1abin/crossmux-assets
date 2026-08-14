#!/usr/bin/env python3
"""Build the reviewed CrossMux StarDict resources from pinned upstream archives."""

from __future__ import annotations

import argparse
import csv
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
import zlib
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
    Source(
        dictionary_id="ecdict-en-zh",
        filename="ecdict.csv",
        url=(
            "https://raw.githubusercontent.com/skywind3000/ECDICT/"
            "82c9872576b23118d7c42e920c11beb77f510ae2/ecdict.csv"
        ),
        sha256="1a6947e04785db63613a92e14903cdae7954f7e84860b10e68e5c7cbb3f9c3cf",
    ),
    Source(
        dictionary_id="century-en-zh",
        filename="stardict-21shijishuangxiangcidian-2.4.2.tar.bz2",
        url=(
            "https://stardict.uber.space/zh_CN/"
            "stardict-21shijishuangxiangcidian-2.4.2.tar.bz2"
        ),
        sha256="718e15eb91e92294f8663e911f03d14b220e52bebe2e7e57565c107162ef8b7b",
    ),
    Source(
        dictionary_id="langdao-en-zh",
        filename="stardict-langdao-ec-gb-2.4.2.tar.bz2",
        url="https://stardict.uber.space/zh_CN/stardict-langdao-ec-gb-2.4.2.tar.bz2",
        sha256="68adfd6348418725b1810b5aeda2506ce44ddbc1ad99f2f68f6ba135cd3bf03c",
    ),
    Source(
        dictionary_id="lazyworm-en-zh",
        filename="stardict-lazyworm-ec-2.4.2.tar.bz2",
        url="https://stardict.uber.space/zh_CN/stardict-lazyworm-ec-2.4.2.tar.bz2",
        sha256="38e5877f48c71df393337d5b5b5b3814cb3477c9d202395adc6d0c47d3ed0a81",
    ),
    Source(
        dictionary_id="quick-en-zh-specialized",
        filename="stardict-quick_eng-zh_CN-2.4.2.tar.bz2",
        url="https://stardict.uber.space/zh_CN/stardict-quick_eng-zh_CN-2.4.2.tar.bz2",
        sha256="488d9ea8ea92c86489409cbd0db4a6568e08a435b7e73ec2821fb8a781923627",
    ),
    Source(
        dictionary_id="xinhua-zh-zh",
        filename="stardict-xhzd-2.4.2.tar.bz2",
        url="https://stardict.uber.space/zh_CN/stardict-xhzd-2.4.2.tar.bz2",
        sha256="24745da6439f7aafd540661aa2cc20096c6fb7aca24dc62a1fb4b65e0822e646",
    ),
    Source(
        dictionary_id="modern-chinese-zh-zh",
        filename="stardict-xiandaihanyucidian_fix-2.4.2.tar.bz2",
        url=(
            "https://stardict.uber.space/zh_CN/"
            "stardict-xiandaihanyucidian_fix-2.4.2.tar.bz2"
        ),
        sha256="1dcf68f876bcecfd2c391f9a6232c3fafba6e00d2db50c302ffd8ca813842ec5",
    ),
)

STARDICT_SOURCES = {
    "century-en-zh": ("21shijishuangxiangcidian", "21世纪英汉汉英双向词典", "2007-01-17"),
    "langdao-en-zh": ("langdao-ec-gb", "朗道英汉字典 5.0", "2003-08-26"),
    "lazyworm-en-zh": ("lazyworm-ec", "懒虫简明英汉词典", "2006-05-17"),
    "quick-en-zh-specialized": ("quick_eng-zh_CN", "英汉专业词典", "2005-10-09"),
    "xinhua-zh-zh": ("xhzd", "新华字典", "unknown"),
    "modern-chinese-zh-zh": ("xiandaihanyucidian_fix", "现代汉语词典（修正版）", "2007-07-08"),
}

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


def import_stardict(archive: Path, stem: str) -> dict[str, list[str]]:
    with tarfile.open(archive, "r:bz2") as bundle:
        members = {Path(member.name).name: member for member in bundle.getmembers() if member.isfile()}
        try:
            index = bundle.extractfile(members[f"{stem}.idx"])
            dictionary = bundle.extractfile(members[f"{stem}.dict.dz"])
            metadata = bundle.extractfile(members[f"{stem}.ifo"])
        except KeyError as exc:
            raise ImportError(f"missing StarDict archive member: {exc}") from exc
        if index is None or dictionary is None or metadata is None:
            raise ImportError("StarDict archive contains a non-file member")
        index_data = index.read()
        dictionary_data = gzip.decompress(dictionary.read())
        ifo = metadata.read().decode("utf-8")

    values = dict(line.partition("=")[::2] for line in ifo.splitlines() if "=" in line)
    sequence = values.get("sametypesequence")
    if sequence not in {"m", "h"}:
        raise ImportError(f"unsupported StarDict sametypesequence: {sequence!r}")

    entries: dict[str, list[str]] = defaultdict(list)
    for headword, offset, size in parse_stardict_index(index_data):
        if size == 0 or offset + size > len(dictionary_data):
            raise ImportError(f"StarDict definition is out of bounds: {headword!r}")
        try:
            definition = dictionary_data[offset : offset + size].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ImportError(f"StarDict definition is not UTF-8: {headword!r}") from exc
        definition = html_to_text(definition) if sequence == "h" else definition
        if definition.strip():
            append_definition(entries, headword, definition)
    return entries


def import_ecdict(source: Path) -> dict[str, list[str]]:
    entries: dict[str, list[str]] = defaultdict(list)
    with source.open("r", encoding="utf-8", newline="") as stream:
        for row in csv.DictReader(stream):
            headword = row.get("word", "").strip()
            translation = row.get("translation", "").replace("\\n", "\n").strip()
            if not headword or not translation:
                continue
            phonetic = row.get("phonetic", "").strip()
            definition = f"/{phonetic}/\n{translation}" if phonetic else translation
            append_definition(entries, headword, definition)
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
    dictzip: bool = False,
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
    plain_path = directory / f"{dictionary_id}.dict"
    dictzip_path = directory / f"{dictionary_id}.dict.dz"
    (plain_path if dictzip else dictzip_path).unlink(missing_ok=True)
    if dictzip:
        write_dictzip(dictzip_path, dictionary)
    else:
        plain_path.write_bytes(dictionary)
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


def write_dictzip(path: Path, data: bytes, chunk_length: int = 58315) -> None:
    chunks = []
    compressor = zlib.compressobj(9, zlib.DEFLATED, -zlib.MAX_WBITS)
    for offset in range(0, len(data), chunk_length):
        chunk = compressor.compress(data[offset : offset + chunk_length])
        last = offset + chunk_length >= len(data)
        chunk += compressor.flush(zlib.Z_FINISH if last else zlib.Z_FULL_FLUSH)
        if len(chunk) > 0xFFFF:
            raise ImportError("dictzip compressed chunk exceeds 65535 bytes")
        chunks.append(chunk)

    ra = struct.pack("<HHH", 1, chunk_length, len(chunks)) + b"".join(
        struct.pack("<H", len(chunk)) for chunk in chunks
    )
    extra = b"RA" + struct.pack("<H", len(ra)) + ra
    header = b"\x1f\x8b\x08\x04\0\0\0\0\0\x03" + struct.pack("<H", len(extra)) + extra
    trailer = struct.pack("<II", zlib.crc32(data) & 0xFFFFFFFF, len(data) & 0xFFFFFFFF)
    path.write_bytes(header + b"".join(chunks) + trailer)


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
    source_by_id = {source.dictionary_id: source for source in SOURCES}
    for dictionary_id, (stem, name, date) in STARDICT_SOURCES.items():
        source = source_by_id[dictionary_id]
        write_stardict(
            output_root,
            dictionary_id,
            import_stardict(sources[dictionary_id], stem),
            name=name,
            date=date,
            dictzip=True,
            description=(
                f"{name}, GPL as listed by the StarDict catalog, normalized to plain-text StarDict "
                f"by CrossMux; source: {source.url}"
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
    write_stardict(
        output_root,
        "ecdict-en-zh",
        import_ecdict(sources["ecdict-en-zh"]),
        name="ECDICT English-Chinese",
        date="2026-08-13",
        dictzip=True,
        description=(
            "ECDICT base CSV at commit 82c9872576b23118d7c42e920c11beb77f510ae2, converted to "
            "plain-text StarDict by CrossMux. MIT License. Copyright (c) 2025 Linwei. Permission is "
            "hereby granted, free of charge, to any person obtaining a copy of this software and "
            "associated documentation files (the Software), to deal in the Software without "
            "restriction, including without limitation the rights to use, copy, modify, merge, "
            "publish, distribute, sublicense, and/or sell copies of the Software, and to permit "
            "persons to whom the Software is furnished to do so, subject to the following "
            "conditions: The above copyright notice and this permission notice shall be included "
            "in all copies or substantial portions of the Software. THE SOFTWARE IS PROVIDED AS IS, "
            "WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE "
            "WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. "
            "IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR "
            "OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT "
            "OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE."
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
