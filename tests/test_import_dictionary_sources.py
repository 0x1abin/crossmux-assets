from __future__ import annotations

import csv
import gzip
import io
import struct
import sys
import tarfile
import tempfile
import unittest
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import import_dictionary_sources as importer


class DictionaryImportTest(unittest.TestCase):
    def test_converts_html_to_plain_text(self):
        source = (
            '<div>/<font color="gray">bʊk</font>/<br><div><font>noun</font></div>'
            '<ol><li>a written work &amp; publication</li><li>to reserve</li></ol></div>'
        )
        self.assertEqual(
            "/bʊk/\nnoun\n- a written work & publication\n- to reserve",
            importer.html_to_text(source),
        )

    def test_parses_wordnet_and_irregular_exceptions(self):
        entries = defaultdict(list)
        importer.parse_wordnet_data(
            '00000001 00 v 02 go 0 move_on 0 000 | change location; "go home"',
            entries,
        )
        importer.parse_wordnet_exceptions("went go\n", entries)
        self.assertIn("go", entries)
        self.assertEqual(entries["go"], entries["went"])
        self.assertIn("Synonyms: go, move on", entries["go"][0])

    def test_imports_freedict_and_merges_duplicate_headwords(self):
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "source.tar.xz"
            definitions = [b"<div>first &amp; one</div>", b"<div>second</div>"]
            dictionary = b"".join(definitions)
            index = bytearray()
            offset = 0
            for definition in definitions:
                index.extend(b"Book\0")
                index.extend(struct.pack(">II", offset, len(definition)))
                offset += len(definition)
            with tarfile.open(archive, "w:xz") as bundle:
                for name, data in (
                    ("eng-zho/eng-zho.idx.gz", gzip.compress(index)),
                    ("eng-zho/eng-zho.dict", dictionary),
                ):
                    info = tarfile.TarInfo(name)
                    info.size = len(data)
                    bundle.addfile(info, io.BytesIO(data))

            entries = importer.import_freedict(archive)
            self.assertEqual(["first & one", "second"], entries["book"])

    def test_imports_ecdict_chinese_definitions(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "ecdict.csv"
            with source.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=("word", "phonetic", "translation"))
                writer.writeheader()
                writer.writerow({"word": "Book", "phonetic": "bʊk", "translation": "n. 书\\nv. 预订"})
                writer.writerow({"word": "empty", "phonetic": "", "translation": ""})

            entries = importer.import_ecdict(source)
            self.assertEqual(["/bʊk/\nn. 书\nv. 预订"], entries["book"])
            self.assertNotIn("empty", entries)

    def test_imports_stardict_and_converts_html(self):
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "source.tar.bz2"
            definitions = [b"<div>first &amp; one</div>", b"<p>second</p>", b"<img src='x.png'>"]
            dictionary = b"".join(definitions)
            index = bytearray()
            offset = 0
            for headword, definition in zip(("Book", "book", "image-only"), definitions):
                index.extend(headword.encode("utf-8") + b"\0")
                index.extend(struct.pack(">II", offset, len(definition)))
                offset += len(definition)
            with tarfile.open(archive, "w:bz2") as bundle:
                for name, data in (
                    ("fixture/fixture.idx", index),
                    ("fixture/fixture.dict.dz", gzip.compress(dictionary)),
                    ("fixture/fixture.ifo", b"sametypesequence=h\n"),
                ):
                    info = tarfile.TarInfo(name)
                    info.size = len(data)
                    bundle.addfile(info, io.BytesIO(data))

            entries = importer.import_stardict(archive, "fixture")
            self.assertEqual(["first & one", "second"], entries["book"])
            self.assertNotIn("image-only", entries)

    def test_writes_deterministic_plain_text_stardict(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            entries = {"Zoo": ["third"], "apple": ["first"], "Banana": ["second"]}
            arguments = dict(name="Fixture", date="2026-01-01", description="Fixture source")
            importer.write_stardict(root / "one", "fixture", entries, **arguments)
            importer.write_stardict(root / "two", "fixture", entries, **arguments)
            first = root / "one" / "fixture"
            second = root / "two" / "fixture"
            for suffix in ("idx", "dict", "ifo"):
                self.assertEqual(
                    (first / f"fixture.{suffix}").read_bytes(),
                    (second / f"fixture.{suffix}").read_bytes(),
                )
            index = (first / "fixture.idx").read_bytes()
            self.assertLess(index.index(b"apple\0"), index.index(b"Banana\0"))
            self.assertIn(b"sametypesequence=m", (first / "fixture.ifo").read_bytes())

    def test_writes_deterministic_dictzip(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = (b"dictionary data\n" * 10000) + b"end"
            importer.write_dictzip(root / "one.dz", data)
            importer.write_dictzip(root / "two.dz", data)
            self.assertEqual((root / "one.dz").read_bytes(), (root / "two.dz").read_bytes())
            with gzip.open(root / "one.dz", "rb") as stream:
                self.assertEqual(data, stream.read())

    def test_rejects_oversized_definition_and_bad_checksum(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(importer.ImportError, "larger"):
                importer.write_stardict(
                    root,
                    "large",
                    {"word": ["x" * (importer.MAX_DEFINITION_BYTES + 1)]},
                    name="Large",
                    date="2026-01-01",
                    description="Fixture source",
                )
            archive = root / "archive"
            archive.write_bytes(b"wrong")
            source = importer.Source("fixture", "archive", "https://example.com/archive", "0" * 64)
            with self.assertRaisesRegex(importer.ImportError, "checksum mismatch"):
                importer.verify_source(source, archive)


if __name__ == "__main__":
    unittest.main()
