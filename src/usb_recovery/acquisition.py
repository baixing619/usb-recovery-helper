"""Conservative GNU ddrescue acquisition into new ordinary files only."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import threading
import time
import uuid

from .engine.image import MapRanges, no_reparse, regular_file
from .engine.exfat import _output_tree
from .process_guard import ProcessGuard
from .windows_devices import (SafetyError, check_source_access, cygwin_device_path,
    device_fingerprint, enumerate_devices, guard_destination, matching_device, read_source_prefix, powershell_path)


def _limited_bytes(path,limit):
    with regular_file(path).open('rb') as source:
        data=source.read(limit+1)
    if len(data)>limit:
        raise SafetyError('任务记录超过安全大小上限。')
    return data


def source_identity_matches(saved, current):
    if saved.get('kind') != current.get('kind'):
        return False
    if current.get('kind') == 'device':
        return bool(current.get('fingerprint')) and saved.get('fingerprint') == current.get('fingerprint')
    return saved.get('identity') == current.get('identity')


class JobFileLock:
    """OS-owned byte lock releases on crash, preventing two writers to one map."""
    def __init__(self, path):
        self.path, self.handle = Path(path), None

    def __enter__(self):
        no_reparse(self.path)
        if self.path.exists() and (not self.path.is_file() or self.path.stat().st_nlink != 1):
            raise SafetyError('任务锁不是独立普通文件。')
        self.handle = self.path.open('a+b',buffering=0)
        try:
            if os.fstat(self.handle.fileno()).st_nlink != 1:
                raise SafetyError('任务锁链接异常。')
            if os.fstat(self.handle.fileno()).st_size == 0:
                self.handle.write(b'\0')
            self.handle.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.handle.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BaseException as error:
            self.handle.close(); self.handle=None
            raise SafetyError('该采集任务正在被使用，或无法取得独占任务锁。') from error
        return self

    def __exit__(self,*_):
        if self.handle is not None:
            try:
                self.handle.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(self.handle.fileno(),msvcrt.LK_UNLCK,1)
                else:
                    import fcntl
                    fcntl.flock(self.handle.fileno(),fcntl.LOCK_UN)
            finally:
                self.handle.close(); self.handle=None


def summarize_map(text: str, expected_size: int) -> dict:
    ranges = MapRanges(text)
    if ranges.end != expected_size:
        raise ValueError('镜像map范围与设备容量不一致。')
    states = {s: 0 for s in '+?*/-'}
    for _, size, state in ranges.parts:
        states[state] += size
    return dict(copied_bytes=states['+'], expected_bytes=expected_size,
                progress=100 * states['+'] / expected_size, complete=states['+'] == expected_size,
                states=states, has_map=True)


def read_map_progress(path: Path, expected_size: int, retry=True) -> dict:
    for attempt in range(8 if retry else 1):
        try:
            no_reparse(path)
            if path.stat().st_size > 64 * 1024 * 1024:
                raise ValueError('map文件超出安全上限。')
            return summarize_map(_limited_bytes(path,64*1024**2).decode('utf-8-sig'), expected_size)
        except (OSError, ValueError):
            if retry and attempt < 7:
                time.sleep(.05)
    return dict(copied_bytes=0, expected_bytes=expected_size, progress=0, complete=False,
                states={}, has_map=False)


def build_command(executable, source, image, map_path, size, sector_size=512, max_read_rate=20*1024**2):
    if type(size) is not int or not 0 < size < 2**63 or sector_size not in (512,1024,2048,4096):
        raise ValueError('采集容量/扇区参数不合法。')
    if type(max_read_rate) is not int or not 4096 <= max_read_rate <= 1024**3:
        raise ValueError('采集速度上限不合法。')
    paths = list(map(str, (source, image, map_path)))
    if len(set(paths)) != 3 or any('\x00' in p or '\n' in p for p in paths):
        raise ValueError('源、镜像和map必须为互不相同的有效路径。')
    return [str(executable), '--quiet', '--cpass=1', '--no-trim', '--no-sweep', '--no-scrape',
            '--retry-passes=0', '--max-read-errors=2', '--timeout=15s', '--mapfile-interval=1s,5s',
            f'--sector-size={sector_size}', f'--cluster-size={65536//sector_size}',
            f'--max-read-rate={max_read_rate}', f'--size={size}', '--', *paths]


def _json_write(path, data):
    no_reparse(path)
    if path.exists() and (not path.is_file() or path.stat().st_nlink != 1):
        raise SafetyError('任务记录不是独立普通文件，已停止。')
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    with temporary.open('x', encoding='utf-8') as output:
        json.dump(data, output, ensure_ascii=False, indent=2)
        output.flush(); os.fsync(output.fileno())
    os.replace(temporary, path)


def _file_identity(path):
    path = regular_file(path)
    info = path.stat()
    with path.open('rb') as source:
        prefix = hashlib.sha256(source.read(65536)).hexdigest()
    return dict(path=str(path), size=info.st_size, mtime_ns=info.st_mtime_ns,
                inode=info.st_ino, device=info.st_dev, prefix_sha256=prefix)


_CYGWIN_WRITABLE = ('source.img','source.map','source.map.bak','source.map.tmp',
                    'source.map.bak.tmp','events.log','rates.log','engine.log')


def _writable_snapshot(directory):
    """Only independently owned files may be reopened by the native writer."""
    rows={}
    for name in _CYGWIN_WRITABLE:
        path=directory/name
        no_reparse(path)
        if not path.exists():continue
        regular_file(path)
        info=path.stat()
        if info.st_nlink != 1:
            raise SafetyError('任务可写文件不是独立普通文件，拒绝写入：'+name)
        rows[name]=[info.st_dev,info.st_ino]
    return rows


def resume_available(directory):
    """Read-only eligibility; source device identity is rechecked when started."""
    try:
        directory=Path(directory)
        saved=json.loads(_limited_bytes(directory/'acquisition.json',4*1024**2).decode('utf-8-sig'))
        if saved.get('state') not in ('cancelled','incomplete') or not saved.get('map_sha256'):
            return False
        actual=_writable_snapshot(directory)
        if any(saved.get('owned_outputs',{}).get(name)!=identity for name,identity in actual.items()):return False
        image=(directory/'source.img').stat()
        if [image.st_dev,image.st_ino] != saved['image_identity'] or [image.st_size,image.st_mtime_ns] != saved.get('image_stat'):return False
        data=_limited_bytes(directory/'source.map',64*1024**2)
        if hashlib.sha256(data).hexdigest()!=saved['map_sha256']:return False
        if not summarize_map(data.decode('utf-8-sig'),saved['expected_bytes'])['has_map']:return False
        return bool(saved['source']['kind']=='file' or saved.get('source_prefix'))
    except (OSError,ValueError,KeyError,TypeError):
        return False


class AcquisitionRunner:
    def __init__(self, engine_dir, max_read_rate=20*1024**2):
        self.engine_dir = Path(engine_dir)
        self.max_read_rate = max_read_rate

    def _env(self):
        env = os.environ.copy()
        for key in ('CYGWIN', 'LD_PRELOAD', 'LD_LIBRARY_PATH'):
            env.pop(key, None)
        env['LC_ALL'] = 'C.UTF-8'; env['LANG'] = 'C.UTF-8'
        system = powershell_path().parents[3]
        env['PATH'] = os.pathsep.join((str(self.engine_dir), str(system/'System32'), str(system)))
        return env

    def verify_engine(self):
        resources = self.engine_dir.parent.parent
        manifest = resources / 'engine-manifest.json'
        rows = json.loads(_limited_bytes(manifest,4*1024**2).decode('utf-8-sig'))
        for row in rows['files']:
            path = resources / row['path']
            if not path.resolve().is_relative_to(resources.resolve()):
                raise SafetyError('恢复引擎资源清单路径异常。')
            regular_file(path)
            if path.stat().st_size != row['bytes'] or hashlib.sha256(path.read_bytes()).hexdigest().lower() != row['sha256'].lower():
                raise SafetyError('恢复引擎不完整或校验不符，请重新解压完整软件包。')
        for name in ('ddrescue.exe','kill.exe','cygpath.exe','cygwin1.dll'):
            if not (self.engine_dir/name).is_file():
                raise SafetyError('恢复引擎缺少必要文件，请重新解压完整软件包。')
        return rows

    def _tool(self, name, args):
        process = subprocess.run([str(self.engine_dir/name), *args], stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10,
            env=self._env(), creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        if process.returncode:
            raise SafetyError('恢复引擎未能解析本地路径，未开始读取。')
        return process.stdout.decode('utf-8-sig').strip()

    def _cygpath(self, path):
        absolute = Path(path).absolute()
        value = self._tool('cygpath.exe', ['-u', str(absolute)])
        back = self._tool('cygpath.exe', ['-w', value])
        if os.path.normcase(os.path.abspath(back)) != os.path.normcase(str(absolute)):
            raise SafetyError('引擎路径转换不一致，未开始读取。')
        return value

    def _interrupt(self, process):
        if process.poll() is None:
            # Exact Popen Windows PID, never an enumerated/untrusted PID and never --force.
            self._tool('kill.exe', ['-s', 'INT', '-W', str(process.pid)])

    def run(self, source, output_dir, size=None, sector_size=512, cancel=None, progress=None, resume=False):
        output_dir=Path(output_dir).absolute()
        no_reparse(output_dir)
        if not output_dir.is_dir():
            raise SafetyError('采集任务文件夹不存在。')
        # Must precede even output guard/lock creation. The Controller's guard
        # is not a substitute for this boundary when the runner is called alone.
        if isinstance(source,dict):
            preflight=matching_device(source,enumerate_devices())
            copied=read_map_progress(output_dir/'source.map',preflight['size'])['copied_bytes'] if resume else 0
            guard_destination(output_dir,preflight,max(0,preflight['size']-copied)+64*1024**2,
                              app_path=Path(sys.executable))
            check_source_access(preflight)
        if not isinstance(source,dict) and Path(source).resolve()==(output_dir/'.acquisition.lock').resolve():
            raise SafetyError('源文件不能是输出任务锁。')
        with _output_tree(output_dir,()), JobFileLock(output_dir/'.acquisition.lock'):
            return self._run(source,output_dir,size,sector_size,cancel,progress,resume)

    def _run(self, source, output_dir, size=None, sector_size=512, cancel=None, progress=None, resume=False):
        cancel = cancel if cancel is not None else threading.Event()
        output_dir = Path(output_dir).absolute()
        no_reparse(output_dir)
        if not output_dir.is_dir():
            raise SafetyError('采集任务文件夹不存在。')
        image, map_path, record = (output_dir/name for name in ('source.img','source.map','acquisition.json'))
        if isinstance(source, dict):
            current = matching_device(source, enumerate_devices())
            check_source_access(current)
            size = current['size']
            sector_size = current.get('logical_sector_size',512)
            copied = read_map_progress(map_path,size)['copied_bytes'] if resume else 0
            guard_destination(output_dir, current, max(0,size-copied)+64*1024**2, app_path=Path(sys.executable))
            source_identity = dict(kind='device', fingerprint=device_fingerprint(current), device=current)
            source_argument = cygwin_device_path(current)
        else:
            source_file = regular_file(source)
            source_identity = dict(kind='file', identity=_file_identity(source_file))
            size = source_file.stat().st_size if size is None else size
            if size != source_file.stat().st_size:
                raise SafetyError('普通文件大小已变化。')
            if source_file.resolve() in (image.resolve(),map_path.resolve(),record.resolve()):
                raise SafetyError('源文件不能与输出文件相同。')
            source_argument = None
        if not size or size % sector_size:
            raise SafetyError('源大小必须为非零的完整扇区倍数。')
        self.verify_engine()
        with _output_tree(output_dir, ()):
            if resume:
                saved = json.loads(_limited_bytes(record,4*1024**2).decode('utf-8-sig'))
                if saved.get('schema') != 1 or not source_identity_matches(saved.get('source',{}),source_identity) or saved.get('expected_bytes') != size:
                    raise SafetyError('源身份或任务记录不符，拒绝续接。')
                if saved.get('state') == 'running':
                    raise SafetyError('任务曾异常退出，缺少闭合快照；保留镜像，但不自动续接。')
                data=_limited_bytes(map_path,64*1024**2)
                if not saved.get('map_sha256') or hashlib.sha256(data).hexdigest()!=saved['map_sha256']:
                    raise SafetyError('读取记录与原任务停止快照不符，拒绝续接。')
                current_outputs=_writable_snapshot(output_dir)
                if any(saved.get('owned_outputs',{}).get(name)!=identity for name,identity in current_outputs.items()):
                    raise SafetyError('任务可写文件已被替换或不属于本任务，拒绝续接。')
                regular_file(image)
                if image.stat().st_nlink != 1 or not read_map_progress(map_path,size)['has_map']:
                    raise SafetyError('镜像或map无法确认，拒绝续接。')
                previous = saved.get('image_identity')
                now = image.stat()
                if previous != [now.st_dev,now.st_ino]:
                    raise SafetyError('任务镜像已被替换，拒绝覆盖。')
                finished_stat = saved.get('image_stat')
                if saved.get('state') != 'running' and finished_stat is not None and finished_stat != [now.st_size,now.st_mtime_ns]:
                    raise SafetyError('任务镜像在暂停后发生变化，拒绝续接。')
                if source_identity['kind'] == 'device':
                    prefix = saved.get('source_prefix')
                    if not prefix:
                        raise SafetyError('原镜像首部未成功读取，无法自动核实续接设备。')
                    if hashlib.sha256(read_source_prefix(current,prefix['bytes'])).hexdigest() != prefix['sha256']:
                        raise SafetyError('原U盘内容指纹与任务不符，拒绝续接。')
            else:
                owned_names=('acquisition.json',*_CYGWIN_WRITABLE)
                if any((output_dir/name).exists() for name in owned_names):
                    raise SafetyError('任务文件已经存在，请使用续接或另建任务，不能覆盖。')
                with image.open('xb') as initial:
                    created=os.fstat(initial.fileno())
                saved = dict(schema=1, source=source_identity, expected_bytes=size, state='new',
                             image_identity=[created.st_dev,created.st_ino])
            if cancel.is_set():
                saved['state']='cancelled';_json_write(record,saved)
                return self._result(image,map_path,size,True,None,saved)
            source_argument = source_argument or self._cygpath(source_file)
            arguments = build_command(self.engine_dir/'ddrescue.exe',source_argument,
                self._cygpath(image),self._cygpath(map_path),size,sector_size,self.max_read_rate)
            arguments[1:1] = ['--log-events='+self._cygpath(output_dir/'events.log'),
                               '--log-rates='+self._cygpath(output_dir/'rates.log')]
            saved['state']='running';_json_write(record,saved)
            log_path = output_dir/'engine.log'
            if log_path.exists():
                regular_file(log_path)
                if log_path.stat().st_nlink != 1:
                    raise SafetyError('引擎日志不是独立文件。')
            with image.open('r+b',buffering=0) as image_pin, log_path.open('ab' if resume else 'xb',buffering=0) as log, ProcessGuard() as job:
                pinned=os.fstat(image_pin.fileno())
                if pinned.st_nlink != 1 or saved['image_identity'] != [pinned.st_dev,pinned.st_ino]:
                    raise SafetyError('开始采集前镜像被替换，未写入任何采集数据。')
                if resume and saved.get('source_prefix'):
                    prefix=saved['source_prefix'];image_pin.seek(0)
                    if hashlib.sha256(image_pin.read(prefix['bytes'])).hexdigest()!=prefix['sha256']:
                        raise SafetyError('任务镜像首部发生变化，拒绝续接。')
                process = subprocess.Popen(arguments,stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,
                    cwd=str(output_dir),env=self._env(),creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                try:
                    job.assign(process)
                except BaseException:
                    # Failure to contain the child is fail-closed; ask it to save and exit.
                    self._interrupt(process)
                    process.wait()
                    raise
                interrupted = False
                try:
                    while process.poll() is None:
                        if cancel.is_set() and not interrupted:
                            self._interrupt(process); interrupted=True
                        state = read_map_progress(map_path,size)
                        if state['has_map'] and not saved.get('source_prefix'):
                            try:
                                snapshot=MapRanges(_limited_bytes(map_path,64*1024**2).decode('utf-8-sig'))
                                if snapshot.permits(0,sector_size):
                                    image_pin.seek(0);prefix=image_pin.read(sector_size)
                                    if len(prefix)==sector_size:
                                        saved['source_prefix']=dict(bytes=sector_size,sha256=hashlib.sha256(prefix).hexdigest())
                                        _json_write(record,saved)
                            except (ValueError,OSError):
                                pass
                        if progress:
                            progress(dict(state, message='正在等待系统结束当前读取并保存进度。' if interrupted else '正在只读保存镜像。'))
                        time.sleep(.2)
                except BaseException:
                    self._interrupt(process)
                    process.wait()
                    raise
                code=process.wait()
                final_info=os.fstat(image_pin.fileno())
                saved['image_stat']=[final_info.st_size,final_info.st_mtime_ns]
            current_image=image.stat()
            if saved['image_identity'] != [current_image.st_dev,current_image.st_ino]:
                raise SafetyError('采集结束时镜像身份发生变化，未标为完整。')
            saved['image_stat']=[current_image.st_size,current_image.st_mtime_ns]
            if source_identity['kind']=='file' and _file_identity(source_file)!=source_identity['identity']:
                saved['state']='source_changed';_json_write(record,saved)
                raise SafetyError('读取期间源文件发生变化，镜像未标为完整。')
            final = read_map_progress(map_path,size)
            ranges = MapRanges(_limited_bytes(map_path,64*1024**2).decode('utf-8-sig')) if final['has_map'] else None
            if ranges is not None and ranges.permits(0,sector_size) and image.stat().st_size>=sector_size:
                with image.open('rb') as contents:
                    saved['source_prefix']=dict(bytes=sector_size,sha256=hashlib.sha256(contents.read(sector_size)).hexdigest())
            if map_path.exists():
                saved['map_sha256']=hashlib.sha256(_limited_bytes(map_path,64*1024**2)).hexdigest()
            saved['owned_outputs']=_writable_snapshot(output_dir)
            result = self._result(image,map_path,size,cancel.is_set(),code,saved)
            saved.update(state='complete' if result['complete'] else 'cancelled' if result['cancelled'] else 'incomplete',
                         complete=result['complete'],copied_bytes=result['copied_bytes'],exit_code=code)
            _json_write(record,saved)
            return result

    @staticmethod
    def _result(image,map_path,size,cancelled,code,saved):
        state=read_map_progress(map_path,size)
        complete=bool(state['complete'] and image.stat().st_size==size and code==0)
        can_resume=bool(state['has_map'] and saved.get('map_sha256') and not complete
                        and (saved['source']['kind']=='file' or saved.get('source_prefix')))
        return dict(state,image_path=str(image),map_path=str(map_path) if state['has_map'] else None,
                    complete=complete,cancelled=cancelled,exit_code=code,can_resume=can_resume)
