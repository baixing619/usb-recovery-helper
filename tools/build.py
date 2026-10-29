"""Build into a new owned directory; never bundles case material or development state."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from datetime import datetime

ROOT=Path(__file__).resolve().parents[1]


def main():
    stamp=datetime.now().strftime('%Y%m%d_%H%M%S')
    build=ROOT/'构建'/stamp
    build.mkdir(parents=True,exist_ok=False)
    source_paths=sorted((ROOT/'src').rglob('*.py'))+[ROOT/'launcher.py',ROOT/'resources'/'engine-manifest.json']
    source_hashes={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in source_paths}
    env=os.environ.copy();env['PYINSTALLER_CONFIG_DIR']=str(ROOT/'构建'/'pyinstaller-cache')
    command=[sys.executable,'-m','PyInstaller','--onedir','--windowed','--name','U盘恢复助手',
             '--distpath',str(build/'dist'),'--workpath',str(build/'work'),'--specpath',str(build),
             '--paths',str(ROOT/'src'),'--add-data',str(ROOT/'resources')+os.pathsep+'resources',
             '--hidden-import','defusedxml','--hidden-import','olefile',str(ROOT/'launcher.py')]
    with (build/'build.log').open('xb') as log:
        result=subprocess.run(command,cwd=ROOT,env=env,stdout=log,stderr=subprocess.STDOUT)
    if result.returncode:
        raise SystemExit('打包失败，日志保留于 '+str(build/'build.log'))
    if any(hashlib.sha256((ROOT/name).read_bytes()).hexdigest()!=digest for name,digest in source_hashes.items()):
        raise SystemExit('打包期间源码发生变化，请重新构建；本次产物不作为发布版本。')
    app=build/'dist'/'U盘恢复助手'
    sys.path.insert(0,str(ROOT/'src'))
    from usb_recovery.engine.demo import create_demo_image
    (app/'示例').mkdir()
    create_demo_image(app/'示例'/'虚构演示U盘.img')
    for name in ('使用说明.txt','版本与范围.txt'):
        shutil.copyfile(ROOT/'docs'/name,app/name)
    notice=app/'第三方许可证';notice.mkdir()
    for source in (ROOT/'resources'/'licenses').iterdir():
        if source.is_file():shutil.copyfile(source,notice/source.name)
    records=[]
    for path in sorted(app.rglob('*')):
        if path.is_file():
            with path.open('rb') as source: digest=hashlib.file_digest(source,'sha256').hexdigest()
            records.append(dict(path=str(path.relative_to(app)),bytes=path.stat().st_size,sha256=digest))
    manifest=dict(version='0.1.0',app_directory=str(app.relative_to(ROOT)),source_hashes=source_hashes,files=records)
    with (build/'build-manifest.json').open('x',encoding='utf-8') as output:json.dump(manifest,output,ensure_ascii=False,indent=2)
    with (ROOT/'构建'/'latest.json').open('w',encoding='utf-8') as output:json.dump({'build':str(build.relative_to(ROOT)),'app':str(app.relative_to(ROOT))},output,ensure_ascii=False)
    print(json.dumps({'build_dir':str(build),'app_dir':str(app),'files':len(records)},ensure_ascii=False))


if __name__=='__main__':main()
