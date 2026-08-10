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
