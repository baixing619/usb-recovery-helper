"""Package only the verified application and an explicit public source whitelist."""
import hashlib
import json
from pathlib import Path
import shutil
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def checked_zip(path, entries):
    with zipfile.ZipFile(path, 'x', zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for source, name in entries:
            archive.write(source, name)
    with zipfile.ZipFile(path) as archive:
        if archive.testzip() is not None:
            raise RuntimeError('ZIP CRC check failed')
        if archive.namelist() != [name for _, name in entries]:
            raise RuntimeError('ZIP inventory differs')
        for source, name in entries:
            if hashlib.sha256(archive.read(name)).hexdigest() != digest(source):
                raise RuntimeError('ZIP bytes differ: ' + name)


def main():
    pointer = json.loads((ROOT / '构建' / 'latest.json').read_text('utf-8'))
    build = ROOT / pointer['build']
    app = ROOT / pointer['app']
    manifest = json.loads((build / 'build-manifest.json').read_text('utf-8'))
    for name, value in manifest['source_hashes'].items():
        if digest(ROOT / name) != value:
            raise RuntimeError('Source changed since build: ' + name)
    expected = {row['path']: row for row in manifest['files']}
    actual = {str(path.relative_to(app)): path for path in app.rglob('*') if path.is_file()}
    if actual.keys() != expected.keys():
        raise RuntimeError('Built application inventory changed')
    for name, path in actual.items():
        if path.stat().st_size != expected[name]['bytes'] or digest(path) != expected[name]['sha256']:
            raise RuntimeError('Built file changed: ' + name)
    release = ROOT / '发布'
    release.mkdir(exist_ok=True)
    # This directory is the ready-to-run copy for the user; preserve older builds.
    delivered = release / 'U盘恢复助手'
    shutil.copytree(app, delivered)
    runtime_zip = release / 'U盘恢复助手_v0.1.0_Windows_x64.zip'
    runtime_entries = [(path, 'U盘恢复助手/' + path.relative_to(app).as_posix())
                       for path in sorted(actual.values())]
    checked_zip(runtime_zip, runtime_entries)
    public_files = []
    for directory in ('src', 'tests', 'tools', 'resources', 'third_party'):
        public_files.extend(path for path in (ROOT / directory).rglob('*')
                            if path.is_file() and '__pycache__' not in path.parts
                            and path.suffix != '.pyc' and not path.name.endswith('.part'))
    public_files.extend(ROOT / name for name in ('launcher.py', 'requirements.txt', 'requirements-build.txt'))
    public_files.extend(ROOT / 'docs' / name for name in
                        ('使用说明.txt', '版本与范围.txt', '源码构建说明.txt', 'third_party.md'))
    source_zip = release / 'U盘恢复助手_v0.1.0_源码与第三方源码.zip'
    source_entries = [(path, 'U盘恢复助手_源码/' + path.relative_to(ROOT).as_posix())
                      for path in sorted(public_files)]
    checked_zip(source_zip, source_entries)
    hashes = {path.name: {'bytes': path.stat().st_size, 'sha256': digest(path)}
              for path in (runtime_zip, source_zip)}
    (release / 'SHA256.txt').write_text('\n'.join(value['sha256'] + '  ' + name for name, value in hashes.items()) + '\n', encoding='utf-8')
    release_record = dict(version='0.1.0', build=pointer['build'], archives=hashes,
                          application_files=manifest['files'], source_hashes=manifest['source_hashes'],
                          real_user_data_included=False)
    with (release / 'release-manifest.json').open('x', encoding='utf-8') as stream:
        json.dump(release_record, stream, ensure_ascii=False, indent=2)
    shutil.copyfile(ROOT / 'docs' / '使用说明.txt', release / '使用说明.txt')
    print(json.dumps({'runtime': hashes[runtime_zip.name], 'source': hashes[source_zip.name],
                      'runtime_files': len(runtime_entries), 'source_files': len(source_entries)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
