from __future__ import annotations

import json
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import build_dictionary_release as release


class DictionaryReleaseTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def write_catalog(self, entries: list[dict], revision: int = 1):
        catalog = self.root / "catalogs" / "dictionaries.json"
        catalog.parent.mkdir(parents=True)
        catalog.write_text(
            json.dumps({"version": 1, "revision": revision, "dictionaries": entries}), encoding="utf-8"
        )
        return catalog

    @staticmethod
    def entry(dictionary_id: str = "wikdict-en-zh") -> dict:
        return {
            "id": dictionary_id,
            "name": "WikDict English → Chinese",
            "description": "English-Chinese Wiktionary dictionary",
            "languages": ["en", "zh"],
            "revision": 3,
            "license": {"name": "CC BY-SA 3.0", "url": "https://example.com/license"},
            "sourceUrl": "https://example.com/source",
        }

    def write_dictionary(self, dictionary_id: str = "wikdict-en-zh", *, ifo: str | None = None):
        directory = self.root / "dictionaries" / dictionary_id
        directory.mkdir(parents=True)
        (directory / f"{dictionary_id}.idx").write_bytes(b"word\0\0\0\0\0\0\0\x04")
        (directory / f"{dictionary_id}.dict.dz").write_bytes(b"definition")
        if ifo is not None:
            (directory / f"{dictionary_id}.ifo").write_text(ifo, encoding="utf-8")

    def test_builds_and_stages_manifest(self):
        catalog = self.write_catalog([self.entry(), self.entry("aaa")], revision=7)
        self.write_dictionary(ifo="StarDict's dict ifo file\nidxoffsetbits=32\n")
        self.write_dictionary("aaa")
        manifest, files = release.build_manifest(self.root, catalog)

        self.assertEqual(7, manifest["revision"])
        self.assertEqual(["aaa", "wikdict-en-zh"], [item["id"] for item in manifest["dictionaries"]])
        output_files = manifest["dictionaries"][1]["files"]
        idx = next(item for item in output_files if item["name"].endswith(".idx"))
        idx_path = self.root / "dictionaries" / "wikdict-en-zh" / "wikdict-en-zh.idx"
        self.assertEqual(zlib.crc32(idx_path.read_bytes()) & 0xFFFFFFFF, idx["crc32"])
        self.assertEqual(5, len(files))

        staged = self.root / "dist"
        release.stage_release(self.root, catalog, staged)
        self.assertEqual(
            {
                "aaa.idx",
                "aaa.dict.dz",
                "wikdict-en-zh.idx",
                "wikdict-en-zh.dict.dz",
                "wikdict-en-zh.ifo",
                "dictionaries.json",
                "SHA256SUMS",
            },
            {path.name for path in staged.iterdir()},
        )

    def test_rejects_invalid_metadata_and_catalog_mismatch(self):
        entry = self.entry("bad/id")
        catalog = self.write_catalog([entry])
        with self.assertRaisesRegex(release.BuildError, "invalid dictionary id"):
            release.build_manifest(self.root, catalog)

        entry = self.entry()
        del entry["license"]
        catalog.write_text(json.dumps({"version": 1, "revision": 1, "dictionaries": [entry]}), encoding="utf-8")
        with self.assertRaisesRegex(release.BuildError, "license"):
            release.build_manifest(self.root, catalog)

        duplicate = self.entry()
        catalog.write_text(
            json.dumps({"version": 1, "revision": 1, "dictionaries": [duplicate, duplicate]}),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(release.BuildError, "duplicate dictionary id"):
            release.build_manifest(self.root, catalog)

        catalog.write_text(
            json.dumps({"version": 1, "revision": release.MAX_REVISION + 1, "dictionaries": []}),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(release.BuildError, "catalog revision"):
            release.build_manifest(self.root, catalog)

        entry = self.entry()
        entry["revision"] = release.MAX_REVISION + 1
        catalog.write_text(
            json.dumps({"version": 1, "revision": 1, "dictionaries": [entry]}), encoding="utf-8"
        )
        with self.assertRaisesRegex(release.BuildError, "revision"):
            release.build_manifest(self.root, catalog)

    def test_rejects_bad_layout_64_bit_indexes_and_large_files(self):
        catalog = self.write_catalog([self.entry()])
        self.write_dictionary(ifo="comment=" + "x" * 4096 + "\nidxoffsetbits=64\n")
        with self.assertRaisesRegex(release.BuildError, "64-bit"):
            release.build_manifest(self.root, catalog)

        ifo = self.root / "dictionaries" / "wikdict-en-zh" / "wikdict-en-zh.ifo"
        ifo.write_text("idxoffsetbits=32\n", encoding="utf-8")
        extra = ifo.parent / "unexpected.syn"
        extra.write_bytes(b"x")
        with self.assertRaisesRegex(release.BuildError, "unexpected files"):
            release.build_manifest(self.root, catalog)

        extra.unlink()
        data = ifo.parent / "wikdict-en-zh.dict.dz"
        with data.open("wb") as stream:
            stream.truncate(release.MAX_FILE_BYTES)
        with self.assertRaisesRegex(release.BuildError, "must be"):
            release.build_manifest(self.root, catalog)

        data.write_bytes(b"")
        with self.assertRaisesRegex(release.BuildError, "must be"):
            release.build_manifest(self.root, catalog)

        data.unlink()
        with self.assertRaisesRegex(release.BuildError, "exactly one"):
            release.build_manifest(self.root, catalog)

        data.write_bytes(b"definition")
        (ifo.parent / "nested").mkdir()
        with self.assertRaisesRegex(release.BuildError, "unexpected files"):
            release.build_manifest(self.root, catalog)


if __name__ == "__main__":
    unittest.main()
