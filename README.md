# CrossMux Assets

Static downloadable resources for CrossMux. Dictionary metadata and StarDict
files are reviewed here, then published as immutable GitHub Releases and mirrored
byte-for-byte to Gitee.

International CrossMux APIs read releases from
[`0x1abin/crossmux-assets`](https://github.com/0x1abin/crossmux-assets). The China API reads the
matching releases from [`x1abin/crossmux-assets`](https://gitee.com/x1abin/crossmux-assets). Both
deployments expose the selected Release directory to devices; they do not proxy dictionary files
or fall back to the other repository.

## Dictionaries

Source layout:

```text
catalogs/dictionaries.json
dictionaries/<id>/<id>.idx
dictionaries/<id>/<id>.dict.dz  # or .dict
dictionaries/<id>/<id>.ifo      # optional
```

Only dictionaries whose licence permits redistribution are accepted. IDs are
ASCII letters, digits, `_` or `-`, at most 31 bytes. StarDict indexes must be
uncompressed and use 32-bit offsets. Individual files must be smaller than
100,000,000 bytes so the release can be mirrored to Gitee.

The catalog contains:

| ID | Source version | Headwords | License |
| --- | --- | ---: | --- |
| `ecdict-en-zh` | ECDICT `82c9872` | 768,739 | MIT |
| `oewn-en-en` | Open English WordNet 2025 | 131,603 | CC BY 4.0 |
| `freedict-en-zh` | FreeDict/WikDict 2025.11.23 | 24,786 | CC BY-SA 3.0 |

The checked-in resources are deterministic plain-text StarDict conversions.
Open English WordNet exception forms are included as lookup aliases. FreeDict
HTML is converted to text and duplicate headwords are merged because CrossMux
does not render StarDict HTML or load `.syn` files. ECDICT keeps its phonetics
and complete Chinese translations while omitting the English definitions,
exam tags and frequency metadata. ECDICT is Copyright (c) 2025 Linwei.
Its definition data uses dictzip compression so the China mirror can transfer
the large resource reliably while retaining random-access lookups on devices.

Rebuild from the pinned upstream sources and compare the result with the
checked-in resources:

```sh
python3 scripts/import_dictionary_sources.py --check
```

For an offline rebuild, place the three source filenames declared in the import
script in one directory and pass `--source-dir <directory>`. The importer checks
these SHA-256 values before reading any source:

```text
english-wordnet-2025.zip
38b16326159f51853626b7d24a44c453fa88ab33f06fce5ec8fc5996d1c2be93

freedict-eng-zho-2025.11.23.stardict.tar.xz
9dbae6bb5558906cc05f1e573bee2deab8b6e09adfb16fc496288926882435af

ecdict.csv
1a6947e04785db63613a92e14903cdae7954f7e84860b10e68e5c7cbb3f9c3cf
```

Validate and stage a release without network access:

```sh
python3 scripts/build_dictionary_release.py --check
python3 -m unittest discover -s tests -v
python3 scripts/build_dictionary_release.py --output-dir /tmp/crossmux-dictionaries
```

Release tags use `dictionaries-m1-r<N>`. The workflow uploads data files first
and `dictionaries.json` last; clients ignore releases without a complete
manifest. GitHub stays draft and Gitee stays prerelease until their uploaded files pass SHA-256
verification against the same staged release.
