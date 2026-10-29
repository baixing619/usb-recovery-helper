# Third-party engine resources

This bundle includes the Cygwin runtime files needed by GNU ddrescue on Windows. It does not install Cygwin or run package postinstall actions.

## Inventory and verification

`resources/engine-manifest.json` is the public inventory for the bundle:

- `files` lists each included executable and DLL by path relative to `resources`, byte size, and SHA-256.
- `packageArchives` lists the corresponding public Cygwin binary package URLs, archive sizes, and SHA-512 values.
- `sourceURLs` lists the seven unique source archives, their public URLs, exact byte sizes, SHA-512 values, and the binary packages they cover. The two GCC runtime packages share one GCC source archive.
- `licenses` lists the plain-text license files and their SHA-256 values.

The source archives total 117,808,707 bytes. Each archive is downloaded in bounded chunks, checked against trusted setup-index metadata or the embedded public manifest metadata, and moved from its `.part` file to its final name only after both size and SHA-512 match. The per-archive limit is 256 MiB and the combined limit is 300 MiB. A failed or interrupted download leaves a `.part` file under `third_party/sources`; it is not recorded as a verified source archive.

The included license texts are Cygwin's `COPYING`, `CYGWIN_LICENSE`, its LGPL license text (`COPYING.LIB`), and GNU ddrescue's decompressed `COPYING.gz`. The associated source archives are included separately under `third_party/sources`.

The recorded package metadata came from the Cygwin setup index that was previously verified with the official GPG key. This preparation rechecks each downloaded archive against that index's declared size and SHA-512; it does not repeat the index signature check.

## Rebuilding the resource bundle

The preparation script uses only Python's standard library. Give it paths to a trusted, unpacked Cygwin binary directory and its integrity metadata. It does not store those input paths in the manifest or this document.

```text
python tools/prepare_engines.py \
  --reference-bin <trusted-cache>/cygwin/usr/bin \
  --reference-manifest <trusted-cache>/binary-integrity.json \
  --reference-packages <trusted-cache>/packages.json \
  --reference-index <trusted-cache>/setup-kernel.ini \
  --jobs 3 --check-cli
```

The setup index must be the trusted index for the package versions being prepared. The script takes each matching source entry immediately after its binary package entry, deduplicates shared source archives, verifies downloads, copies only the required binaries and license texts, then writes the manifest. It does not use or update the system `PATH`, registry, or other machine configuration.

For a repeat run from a published bundle, use the included manifest for both integrity and package metadata. Stage the exact binaries listed in `files` in a local folder first; the embedded `sourceURLs` records supply source archive checksums and download locations, so the setup index argument can be omitted.

```text
python tools/prepare_engines.py \
  --reference-bin <staging-bin> \
  --reference-manifest resources/engine-manifest.json \
  --reference-packages resources/engine-manifest.json \
  --jobs 3 --check-cli
```

The CLI check runs the copied `ddrescue.exe --version` and `kill.exe --help` with `LC_ALL=C.UTF-8` and `LANG=C.UTF-8`, suppressing console-window creation on Windows.
