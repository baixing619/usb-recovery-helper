"""Exercise the built EXE from a relocated folder without Python on PATH."""
import ctypes
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
from datetime import datetime

ROOT = Path(__file__).resolve().parents[1]


def main():
    pointer = json.loads((ROOT / '构建' / 'latest.json').read_text(encoding='utf-8'))
    source = ROOT / pointer['app']
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    check = ROOT / '验证' / ('迁移路径 含中文 ' + stamp)
    check.mkdir(parents=True, exist_ok=False)
    relocated = check / '独立运行包'
    shutil.copytree(source, relocated)
    output = check / '自检输出'
    output.mkdir()
    cache = check / '隔离临时缓存'
    cache.mkdir()
    buf = ctypes.create_unicode_buffer(32768)
    if not ctypes.windll.kernel32.GetWindowsDirectoryW(buf, len(buf)):
        raise OSError('Cannot discover Windows directory')
    env = os.environ.copy()
    for name in ('PYTHONPATH', 'PYTHONHOME', 'VIRTUAL_ENV', 'CONDA_PREFIX'):
        env.pop(name, None)
    env.update(PATH=str(Path(buf.value) / 'System32'), TEMP=str(cache), TMP=str(cache))
    report = check / 'result.json'
    with (check / 'process.log').open('xb') as log:
        run = subprocess.run([str(relocated / 'U盘恢复助手.exe'), '--self-test',
                              '--output', str(output), '--report', str(report)],
                             cwd=check, env=env, stdout=log, stderr=log,
                             timeout=180, creationflags=subprocess.CREATE_NO_WINDOW)
    result = json.loads(report.read_text(encoding='utf-8')) if report.exists() else {}
    if run.returncode or not result.get('passed'):
        raise RuntimeError(f'Portable EXE self-test failed: {run.returncode}; see {report}')
    manifest = json.loads((ROOT / pointer['build'] / 'build-manifest.json').read_text(encoding='utf-8'))
    for record in manifest['files']:
        target = relocated / record['path']
        with target.open('rb') as stream:
            actual = hashlib.file_digest(stream, 'sha256').hexdigest()
        if actual != record['sha256'] or target.stat().st_size != record['bytes']:
            raise RuntimeError('Relocated file differs: ' + record['path'])
    print(json.dumps({'ok': True, 'report': str(report), 'files_verified': len(manifest['files'])}, ensure_ascii=False))


if __name__ == '__main__':
    main()
