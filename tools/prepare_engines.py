#!/usr/bin/env python3
"""Prepare verified Cygwin/ddrescue binaries, license texts, and source archives.

This script only copies files from explicitly supplied reference paths, downloads
archives listed by the supplied Cygwin setup index (or public engine manifest),
and verifies hashes. It does not install packages or run postinstall scripts.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.error
import urllib.request
from pathlib import Path, PurePosixPath
from typing import Any


BASE_URL = "https://mirrors.kernel.org/sourceware/cygwin/"
CHUNK_BYTES = 1024 * 1024
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_TOTAL_BYTES = 300 * 1024 * 1024
BINARIES = (
    "ddrescue.exe",
    "kill.exe",
    "cygpath.exe",
    "cygwin1.dll",
    "cyggcc_s-seh-1.dll",
    "cygstdc++-6.dll",
    "cygiconv-2.dll",
    "cygintl-8.dll",
    "cygz.dll",
    "cygzstd-1.dll",
    "cygwin-console-helper.exe",
)
PACKAGE_NAMES = (
    "ddrescue",
    "cygwin",
    "libgcc1",
    "libstdc++6",
    "libiconv2",
    "libintl8",
    "libzstd1",
    "zlib0",
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def sha512_file(path: Path) -> str:
    digest = hashlib.sha512()
    with path.open("rb") as stream:
        while chunk := stream.read(CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read JSON input {path.name}: {exc}") from exc


def safe_relative_path(value: str) -> str:
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in ("", ".", "..") for part in path.parts):
        raise ValueError(f"Unsafe relative path in metadata: {value!r}")
    return path.as_posix()


def load_integrity(path: Path) -> dict[str, str]:
    data = read_json(path)
    records: Any
    if isinstance(data, list):
        records = data
    elif isinstance(data, dict) and isinstance(data.get("files"), list):
        records = data["files"]
    else:
        raise ValueError("Reference manifest must be a legacy file list or contain a files list")

    result: dict[str, str] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        filename = record.get("File") or Path(str(record.get("path", ""))).name
        checksum = record.get("SHA256") or record.get("sha256")
        if filename and checksum:
            key = str(filename).lower()
            checksum = str(checksum).lower()
            if not re.fullmatch(r"[0-9a-f]{64}", checksum):
                raise ValueError(f"Invalid SHA-256 for {filename}")
            if key in result and result[key] != checksum:
                raise ValueError(f"Conflicting SHA-256 values for {filename}")
            result[key] = checksum
    missing = [name for name in BINARIES if name.lower() not in result]
    if missing:
        raise ValueError("Reference manifest is missing: " + ", ".join(missing))
    return result


def _package_record(record: dict[str, Any]) -> dict[str, Any]:
    name = record.get("Name") or record.get("name")
    relative = record.get("RelativePath") or record.get("relativePath")
    byte_count = record.get("Bytes", record.get("bytes"))
    digest = record.get("SHA512") or record.get("sha512")
    if not all((name, relative, byte_count is not None, digest)):
        raise ValueError("A package archive record is missing name, relativePath, bytes, or sha512")
    relative = safe_relative_path(str(relative))
    try:
        byte_count = int(byte_count)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid archive size for {name}") from exc
    digest = str(digest).lower()
    if byte_count < 0 or not re.fullmatch(r"[0-9a-f]{128}", digest):
        raise ValueError(f"Invalid size or SHA-512 for {name}")
    return {"name": str(name), "relativePath": relative, "bytes": byte_count, "sha512": digest}


def load_packages(path: Path) -> tuple[dict[str, dict[str, Any]], dict[str, Any] | None]:
    data = read_json(path)
    if isinstance(data, list):
        records = data
        embedded_manifest = None
    elif isinstance(data, dict) and isinstance(data.get("packageArchives"), list):
        records = data["packageArchives"]
        embedded_manifest = data
    else:
        raise ValueError("Reference packages must be a legacy package list or contain packageArchives")

    packages: dict[str, dict[str, Any]] = {}
    for raw in records:
        if not isinstance(raw, dict):
            continue
        record = _package_record(raw)
        name = record["name"]
        if name in packages:
            raise ValueError(f"Duplicate package record: {name}")
        packages[name] = record
    if set(packages) != set(PACKAGE_NAMES):
        missing = sorted(set(PACKAGE_NAMES) - set(packages))
        extra = sorted(set(packages) - set(PACKAGE_NAMES))
        raise ValueError(f"Unexpected package set; missing={missing}, extra={extra}")
    return packages, embedded_manifest


def parse_index(index_path: Path, packages: dict[str, dict[str, Any]]) -> tuple[dict[str, Any], dict[str, str]]:
    by_path = {record["relativePath"]: record for record in packages.values()}
    found: dict[str, Any] = {}
    current_package = ""
    current_version = ""
    try:
        lines = index_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"Cannot read reference index: {exc}") from exc

    for line_number, line in enumerate(lines):
        if line.startswith("@ "):
            current_package = line[2:].strip()
            current_version = ""
            continue
        if line.startswith("version: "):
            current_version = line[len("version: "):].strip()
            continue
        if not line.startswith("install: "):
            continue

        fields = line.split()
        if len(fields) != 4:
            continue
        relative = fields[1]
        expected = by_path.get(relative)
        if expected is None:
            continue
        if line_number + 1 >= len(lines) or not lines[line_number + 1].startswith("source: "):
            raise ValueError(f"No immediately following source entry for {expected['name']}")
        if current_package != expected["name"]:
            raise ValueError(f"Index package name mismatch for {expected['name']}: {current_package}")
        try:
            install_bytes = int(fields[2])
        except ValueError as exc:
            raise ValueError(f"Invalid install size for {expected['name']}") from exc
        install_sha512 = fields[3].lower()
        if (install_bytes, install_sha512) != (expected["bytes"], expected["sha512"]):
            raise ValueError(f"Binary package metadata differs from reference package list: {expected['name']}")

        source_fields = lines[line_number + 1].split()
        if len(source_fields) != 4 or source_fields[0] != "source:":
            raise ValueError(f"Malformed source entry for {expected['name']}")
        source_path = safe_relative_path(source_fields[1])
        try:
            source_bytes = int(source_fields[2])
        except ValueError as exc:
            raise ValueError(f"Invalid source size for {expected['name']}") from exc
        source_sha512 = source_fields[3].lower()
        if source_bytes < 0 or not re.fullmatch(r"[0-9a-f]{128}", source_sha512):
            raise ValueError(f"Invalid source metadata for {expected['name']}")
        source = {
            "relativePath": source_path,
            "url": BASE_URL + source_path,
            "bytes": source_bytes,
            "sha512": source_sha512,
            "packages": [expected["name"]],
        }
        prior = found.get(source_path)
        if prior is not None:
            if (prior["bytes"], prior["sha512"]) != (source_bytes, source_sha512):
                raise ValueError(f"Conflicting metadata for shared source {source_path}")
            prior["packages"].append(expected["name"])
        else:
            found[source_path] = source
        expected["version"] = current_version

    absent = sorted(set(packages) - {name for source in found.values() for name in source["packages"]})
    if absent:
        raise ValueError("No matching install/source entry for: " + ", ".join(absent))
    versions = {name: packages[name].get("version", "") for name in PACKAGE_NAMES}
    return found, versions


def sources_from_public_manifest(manifest: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    raw_sources = manifest.get("sourceURLs")
    if not isinstance(raw_sources, list) or not raw_sources:
        raise ValueError("No --reference-index supplied and public manifest has no sourceURLs list")
    sources: dict[str, Any] = {}
    for raw in raw_sources:
        if not isinstance(raw, dict):
            continue
        relative = safe_relative_path(str(raw.get("relativePath", "")))
        source = {
            "relativePath": relative,
            "url": str(raw.get("url") or BASE_URL + relative),
            "bytes": int(raw.get("bytes", raw.get("size", -1))),
            "sha512": str(raw.get("sha512", "")).lower(),
            "packages": list(raw.get("packages", [])),
        }
        if (source["bytes"] < 0 or source["bytes"] > MAX_ARCHIVE_BYTES
                or not re.fullmatch(r"[0-9a-f]{128}", source["sha512"])):
            raise ValueError(f"Invalid public source metadata for {relative}")
        sources[relative] = source
    versions = manifest.get("versions", {})
    if not isinstance(versions, dict):
        versions = {}
    return sources, {name: str(versions.get(name, "")) for name in PACKAGE_NAMES}


def verify_or_copy(source: Path, destination: Path, expected_sha256: str) -> dict[str, Any]:
    if not source.is_file():
        raise ValueError(f"Reference binary is missing: {source.name}")
    actual = sha256_file(source)
    if actual != expected_sha256:
        raise ValueError(f"Reference binary hash mismatch: {source.name}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if not destination.is_file() or sha256_file(destination) != expected_sha256:
            raise ValueError(f"Refusing to overwrite a different destination file: {destination}")
    else:
        shutil.copyfile(source, destination)
    copied = sha256_file(destination)
    if copied != expected_sha256:
        raise ValueError(f"Copied binary hash mismatch: {destination.name}")
    return {"path": destination.relative_to(RESOURCE_ROOT).as_posix(), "sha256": copied,
            "bytes": destination.stat().st_size}


def save_license_text(content: bytes, destination: Path, provenance: str) -> dict[str, Any]:
    # License texts are plain UTF-8/ASCII; normalize line endings for portable text files.
    text = content.decode("utf-8")
    encoded = text.replace("\r\n", "\n").encode("utf-8")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if not destination.is_file() or destination.read_bytes() != encoded:
            raise ValueError(f"Refusing to overwrite a different license file: {destination.name}")
    else:
        destination.write_bytes(encoded)
    return {"path": destination.relative_to(RESOURCE_ROOT).as_posix(),
            "source": provenance, "sha256": hashlib.sha256(encoded).hexdigest(), "bytes": len(encoded)}


def copy_license(source: Path, destination: Path, provenance: str, compressed: bool = False) -> dict[str, Any]:
    if not source.is_file():
        raise FileNotFoundError(source.name)
    if compressed:
        with gzip.open(source, "rb") as stream:
            content = stream.read()
    else:
        content = source.read_bytes()
    return save_license_text(content, destination, provenance)


def prepare_licenses(reference_bin: Path) -> list[dict[str, Any]]:
    # The reference path is expected to be the usr/bin directory; derive only
    # the neighboring package documentation tree from that explicit argument.
    cygwin_root = reference_bin.parent.parent
    docs = cygwin_root / "usr" / "share" / "doc"
    existing = [
        RESOURCE_ROOT / "licenses" / "Cygwin-COPYING.txt",
        RESOURCE_ROOT / "licenses" / "CYGWIN_LICENSE.txt",
        RESOURCE_ROOT / "licenses" / "ddrescue-COPYING.txt",
    ]
    sources = [
        (docs / "Cygwin" / "COPYING", existing[0], "usr/share/doc/Cygwin/COPYING", False),
        (docs / "Cygwin" / "CYGWIN_LICENSE", existing[1], "usr/share/doc/Cygwin/CYGWIN_LICENSE", False),
        (docs / "ddrescue" / "COPYING.gz", existing[2], "usr/share/doc/ddrescue/COPYING.gz (decompressed)", True),
    ]
    results = []
    for source, destination, provenance, compressed in sources:
        if source.is_file():
            results.append(copy_license(source, destination, provenance, compressed))
        elif destination.is_file():
            content = destination.read_bytes()
            content.decode("utf-8")
            results.append({"path": destination.relative_to(RESOURCE_ROOT).as_posix(),
                            "source": provenance, "sha256": hashlib.sha256(content).hexdigest(),
                            "bytes": len(content)})
        else:
            raise FileNotFoundError(f"Required license text is missing: {source.name}")
    return results


def extract_cygwin_lgpl_license(sources: dict[str, Any]) -> dict[str, Any]:
    cygwin_source = next((item for item in sources.values() if "cygwin" in item.get("packages", [])), None)
    if cygwin_source is None:
        raise ValueError("Cygwin source archive is not in the source manifest")
    archive = SOURCE_ROOT / PurePosixPath(cygwin_source["relativePath"]).name
    destination = RESOURCE_ROOT / "licenses" / "Cygwin-COPYING.LIB.txt"
    try:
        with tarfile.open(archive, "r:xz") as outer:
            nested_members = [
                member for member in outer.getmembers()
                if member.isfile() and "newlib-cygwin" in member.name and member.name.endswith(".tar.bz2")
            ]
            if len(nested_members) != 1:
                raise ValueError("Cygwin source archive has no unique newlib-cygwin source tarball")
            nested_stream = outer.extractfile(nested_members[0])
            if nested_stream is None:
                raise ValueError("Cannot read nested Cygwin source tarball")
            with nested_stream, tarfile.open(fileobj=nested_stream, mode="r|bz2") as inner:
                for member in inner:
                    if member.name == "newlib-cygwin/winsup/COPYING.LIB" and member.isfile():
                        source_stream = inner.extractfile(member)
                        if source_stream is None:
                            break
                        with source_stream:
                            content = source_stream.read()
                        provenance = (
                            f"{cygwin_source['relativePath']}!newlib-cygwin/winsup/COPYING.LIB"
                        )
                        return save_license_text(content, destination, provenance)
    except (OSError, tarfile.TarError) as exc:
        raise ValueError(f"Cannot extract Cygwin LGPL license text: {exc}") from exc
    raise ValueError("Cygwin source archive does not contain its LGPL license text")


def _download_one(source: dict[str, Any], destination: Path) -> dict[str, Any]:
    expected_size = int(source["bytes"])
    expected_hash = str(source["sha512"]).lower()
    if expected_size > MAX_ARCHIVE_BYTES:
        raise ValueError(f"Source archive exceeds per-file limit: {destination.name}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if not destination.is_file():
            raise ValueError(f"Source destination is not a regular file: {destination.name}")
        actual_size = destination.stat().st_size
        actual_hash = sha512_file(destination)
        if actual_size == expected_size and actual_hash == expected_hash:
            return {**source, "filePath": destination.relative_to(PROJECT_ROOT).as_posix(), "downloaded": False}
        raise ValueError(f"Existing source archive failed verification; preserving it: {destination.name}")

    part = destination.with_name(destination.name + ".part")
    offset = part.stat().st_size if part.exists() else 0
    if offset > expected_size:
        raise ValueError(f"Partial file exceeds expected size; preserving it: {part.name}")
    request = urllib.request.Request(
        source["url"],
        headers={"User-Agent": "prepare-engines/1.0", **({"Range": f"bytes={offset}-"} if offset else {})},
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            status = getattr(response, "status", 200)
            append = offset > 0 and status == 206
            if append:
                content_range = response.headers.get("Content-Range", "")
                if not content_range.startswith(f"bytes {offset}-"):
                    raise ValueError(f"Unexpected range response for {destination.name}; partial retained")
            else:
                offset = 0
            digest = hashlib.sha512()
            written = 0
            mode = "ab" if append else "wb"
            if append:
                with part.open("rb") as previous:
                    while chunk := previous.read(CHUNK_BYTES):
                        digest.update(chunk)
                        written += len(chunk)
            with part.open(mode) as output:
                while chunk := response.read(CHUNK_BYTES):
                    written += len(chunk)
                    if written > expected_size or written > MAX_ARCHIVE_BYTES:
                        raise ValueError(f"Download exceeded declared limit; partial retained: {part.name}")
                    output.write(chunk)
                    digest.update(chunk)
                output.flush()
                os.fsync(output.fileno())
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"Download failed; partial retained at {part.name}: {exc}") from exc

    actual_size = part.stat().st_size
    actual_hash = digest.hexdigest()
    if actual_size != expected_size or actual_hash != expected_hash:
        raise ValueError(
            f"Source verification failed; partial retained at {part.name} "
            f"(bytes={actual_size}/{expected_size}, sha512={actual_hash})"
        )
    os.replace(part, destination)
    return {**source, "filePath": destination.relative_to(PROJECT_ROOT).as_posix(), "downloaded": True}


def download_sources(sources: dict[str, Any], jobs: int) -> list[dict[str, Any]]:
    if not sources:
        raise ValueError("No source archives were found")
    records = list(sources.values())
    for source in records:
        source["url"] = source.get("url") or BASE_URL + source["relativePath"]
        if source["url"] != BASE_URL + source["relativePath"]:
            raise ValueError(f"Unexpected source URL for {source['relativePath']}")
        if source["bytes"] < 0 or source["bytes"] > MAX_ARCHIVE_BYTES:
            raise ValueError(f"Source archive exceeds per-file limit: {source['relativePath']}")
    total_size = sum(int(source["bytes"]) for source in records)
    if total_size > MAX_TOTAL_BYTES:
        raise ValueError(f"Source archives exceed total limit: {total_size} bytes")
    print(f"downloading {len(records)} unique source archives ({total_size} bytes; limit {MAX_TOTAL_BYTES})")

    destinations: dict[str, str] = {}
    futures: dict[concurrent.futures.Future[dict[str, Any]], str] = {}
    results: list[dict[str, Any]] = []
    errors: list[str] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(3, jobs))) as pool:
        for source in records:
            filename = PurePosixPath(source["relativePath"]).name
            prior = destinations.get(filename)
            if prior and prior != source["relativePath"]:
                raise ValueError(f"Two source archives have the same basename: {filename}")
            destinations[filename] = source["relativePath"]
            target = SOURCE_ROOT / filename
            futures[pool.submit(_download_one, source, target)] = source["relativePath"]
        for future in concurrent.futures.as_completed(futures):
            relative = futures[future]
            try:
                result = future.result()
                results.append(result)
                print(f"verified source {relative}: {result['bytes']} bytes sha512={result['sha512']}")
            except Exception as exc:  # report every worker outcome and retain .part files
                errors.append(f"{relative}: {exc}")
    if errors:
        raise RuntimeError("Source download/verification failed:\n- " + "\n- ".join(errors))
    return sorted(results, key=lambda item: item["relativePath"])


def check_cli(binary_root: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["LC_ALL"] = "C.UTF-8"
    env["LANG"] = "C.UTF-8"
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    results: dict[str, str] = {}
    for name, args in (("ddrescue.exe", ["--version"]), ("kill.exe", ["--help"])):
        completed = subprocess.run(
            [str(binary_root / name), *args], cwd=binary_root, env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", timeout=30,
            creationflags=creationflags, check=False,
        )
        output = completed.stdout.strip()
        if completed.returncode != 0:
            raise RuntimeError(f"{name} {args[0]} failed ({completed.returncode}): {output[:400]}")
        results[name] = output.splitlines()[0] if output else "(no output)"
        print(f"verified CLI {name} {args[0]}: {results[name]}")
    return results


def build_manifest(
    files: list[dict[str, Any]], licenses: list[dict[str, Any]], packages: dict[str, dict[str, Any]],
    sources: list[dict[str, Any]], versions: dict[str, str], cli_checks: dict[str, str] | None,
) -> dict[str, Any]:
    package_records = []
    for name in PACKAGE_NAMES:
        record = packages[name]
        package_records.append({
            "name": name,
            "version": versions.get(name) or record.get("version", ""),
            "relativePath": record["relativePath"],
            "url": BASE_URL + record["relativePath"],
            "bytes": record["bytes"],
            "sha512": record["sha512"],
        })
    public_sources = []
    for source in sources:
        public_sources.append({key: source[key] for key in
                               ("relativePath", "url", "bytes", "sha512", "packages", "filePath")})
    result: dict[str, Any] = {
        "schemaVersion": 1,
        "versions": {name: value for name, value in versions.items() if value},
        "files": sorted(files, key=lambda item: item["path"]),
        "packageArchives": package_records,
        "sourceURLs": public_sources,
        "licenses": sorted(licenses, key=lambda item: item["path"]),
    }
    if cli_checks is not None:
        result["cliChecks"] = cli_checks
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference-bin", type=Path, required=True,
                        help="Trusted directory containing the reference Cygwin binaries")
    parser.add_argument("--reference-manifest", type=Path, required=True,
                        help="Binary integrity JSON or a previously generated engine manifest")
    parser.add_argument("--reference-packages", type=Path, required=True,
                        help="Package JSON or a previously generated engine manifest")
    parser.add_argument("--reference-index", type=Path,
                        help="Previously verified setup-kernel.ini; optional with a public engine manifest")
    parser.add_argument("--jobs", type=int, default=3, help="Concurrent source downloads (maximum 3)")
    parser.add_argument("--check-cli", action="store_true",
                        help="Run ddrescue --version and kill --help without opening a window")
    args = parser.parse_args(argv)
    if not 1 <= args.jobs <= 3:
        parser.error("--jobs must be between 1 and 3")

    integrity = load_integrity(args.reference_manifest)
    packages, embedded_manifest = load_packages(args.reference_packages)
    sources: dict[str, Any]
    if args.reference_index:
        sources, versions = parse_index(args.reference_index, packages)
    else:
        public_manifest = embedded_manifest or read_json(args.reference_manifest)
        if not isinstance(public_manifest, dict):
            raise ValueError("--reference-index is required for legacy package and integrity JSON inputs")
        sources, versions = sources_from_public_manifest(public_manifest)
        for name in PACKAGE_NAMES:
            version = next((item.get("version", "") for item in packages.values() if item["name"] == name), "")
            if version and not versions.get(name):
                versions[name] = version

    files = []
    for name in BINARIES:
        source = args.reference_bin / name
        destination = RESOURCE_ROOT / "cygwin" / "bin" / name
        files.append(verify_or_copy(source, destination, integrity[name.lower()]))

    downloaded_sources = download_sources(sources, args.jobs)
    licenses = prepare_licenses(args.reference_bin)
    licenses.append(extract_cygwin_lgpl_license(sources))
    cli_checks = check_cli(RESOURCE_ROOT / "cygwin" / "bin") if args.check_cli else None
    manifest = build_manifest(files, licenses, packages, downloaded_sources, versions, cli_checks)
    manifest_path = RESOURCE_ROOT / "engine-manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_manifest = manifest_path.with_suffix(".json.part")
    temporary_manifest.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temporary_manifest, manifest_path)
    print(f"wrote manifest: {manifest_path.relative_to(PROJECT_ROOT).as_posix()}")
    print(f"verified {len(files)} binaries, {len(licenses)} license texts, {len(downloaded_sources)} source archives "
          f"({sum(item['bytes'] for item in downloaded_sources)} bytes total)")
    return 0


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RESOURCE_ROOT = PROJECT_ROOT / "resources"
SOURCE_ROOT = PROJECT_ROOT / "third_party" / "sources"


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"prepare failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
