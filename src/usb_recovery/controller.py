"""Single-job orchestration. Devices are never accessed by the UI directly."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import queue
import shutil
import stat
import subprocess
import sys
import threading
import uuid

from . import reports
from . import windows_devices as win
from .engine import deep_scan, exfat, office
from .engine.image import ImageReader, Cancelled, check_cancel, detect_volumes, no_reparse, regular_file

MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_RECORDS = 20000
OFFICE_SUFFIXES = {'.xls', '.xlsx', '.xlsm', '.xltx', '.xltm', '.doc', '.docx', '.docm',
                   '.dotx', '.dotm', '.ppt', '.pptx', '.pptm', '.ppsx', '.ppsm', '.potx', '.potm'}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _directory(path):
    raw = os.fspath(path)
    if raw.startswith(('\\\\', '//', '\\??\\')) or '\x00' in raw:
        raise ValueError('请选择已有的本地普通文件夹，不接受设备或网络路径')
    target = Path(os.path.abspath(path))
    no_reparse(target)
    if not target.is_dir():
        raise ValueError('请选择一个已经存在的保存文件夹')
    return target


def _atomic_json(job, name, data):
    if name != 'manifest.json':
        raise ValueError('只能更新任务自身的状态记录')
    with exfat._output_tree(job, []):
        target = job / name
        no_reparse(target)
        temporary = job / ('.manifest-' + uuid.uuid4().hex + '.tmp')
        with temporary.open('xb') as output:
            output.write(reports.json_bytes(data))
            output.flush(); os.fsync(output.fileno())
        os.replace(temporary, target)


class _OutputBudget:
    def __init__(self, job, limit, per_file, reserve):
        self.job = Path(job)
        self.limit = limit
        self.per_file = per_file
        self.reserve = reserve
        self.used = 0

    def guard(self, path, size, *, reserve=True):
        target = Path(path).absolute()
        no_reparse(target)
        if not target.is_relative_to(self.job) or type(size) is not int or size < 0:
            raise ValueError('输出路径或文件大小无效')
        if size > self.per_file:
            raise ValueError('单个文件超过本次输出上限；未输出，不以截断内容冒充完整文件')
        if self.used + size > self.limit:
            raise ValueError('达到本次总输出上限，剩余候选未输出')
        if shutil.disk_usage(self.job).free < size + self.reserve:
            raise ValueError('目标剩余空间不足，已保留部分结果')
        if reserve:
            self.used += size


class Controller:
    def __init__(self, engine_dir=None, runner=None, *, max_file_bytes=256 * 1024**3,
                 max_total_bytes=None, reserve_bytes=64 * 1024**2):
        if type(max_file_bytes) is not int or max_file_bytes <= 0:
            raise ValueError('单文件输出上限必须为正整数')
        if max_total_bytes is not None and (type(max_total_bytes) is not int or max_total_bytes <= 0):
            raise ValueError('总输出上限必须为正整数')
        if type(reserve_bytes) is not int or reserve_bytes < 0:
            raise ValueError('空间保留量不能为负')
        bundle_root = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[2]))
        self.engine_dir = Path(engine_dir) if engine_dir else bundle_root / 'resources' / 'cygwin' / 'bin'
        self._runner = runner
        self.max_file_bytes, self.max_total_bytes, self.reserve_bytes = max_file_bytes, max_total_bytes, reserve_bytes
        self.events = queue.Queue()
        self._lock = threading.Lock()
        self._log_lock = threading.Lock()
        self._cancel = threading.Event()
        self._thread = None
        self._manifest = None
        self._job = None
        self._log_identity = None
        self._log_failures = []
        self._status = dict(running=False, stage='idle', job_dir='', can_resume=False)
        self.last_result = None

    def devices(self):
        return win.enumerate_devices()

    def probe_image(self, image, map_path=None):
        with ImageReader(image, map_path=map_path) as reader:
            return detect_volumes(reader)

    def status(self):
        with self._lock:
            return dict(self._status)

    def _reserve(self):
        with self._lock:
            if self._status['running']:
                raise RuntimeError('已有任务正在处理，请先停止或等待结束')
            # Created before preparation, never reset inside a worker or runner.
            self._cancel = threading.Event()
            self._status = dict(running=True, stage='starting', job_dir='', can_resume=False)
            self.last_result = None
            self._job, self._manifest, self._log_identity = None, None, None
            self._log_failures = []

    def _new_job(self, destination, kind, source, options):
        parent = _directory(destination)
        with exfat._output_tree(parent, []):
            job_id = uuid.uuid4().hex
            job = parent / ('恢复任务_' + datetime.now().strftime('%Y%m%d_%H%M%S') + '_' + job_id[:8])
            job.mkdir(exist_ok=False)
        self._job = job
        self._manifest = dict(schema=1, job_id=job_id, kind=kind, created_at=_now(), updated_at=_now(),
                              source=source, options=options, stage='starting', complete=False,
                              cancelled=False, can_resume=False, job_dir=str(job))
        self._initialize_log()
        self._save_manifest()
        with self._lock:
            self._status['job_dir'] = str(job)
        return job

    def _initialize_log(self):
        with exfat._output_tree(self._job, []):
            log = self._job / ('events_' + uuid.uuid4().hex[:10] + '.jsonl')
            with log.open('xb') as output:
                info = os.fstat(output.fileno())
                self._log_identity = (info.st_dev, info.st_ino)
            self._log_path = log

    def _save_manifest(self):
        if self._manifest is not None:
            self._manifest['updated_at'] = _now()
            _atomic_json(self._job, 'manifest.json', self._manifest)

    def _log_event(self, event):
        if self._job is None or self._log_identity is None or event['type'] == 'progress':
            return
        try:
            with self._log_lock, exfat._output_tree(self._job, []):
                no_reparse(self._log_path)
                with self._log_path.open('ab') as output:
                    info = os.fstat(output.fileno())
                    if not stat.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino) != self._log_identity or info.st_nlink != 1:
                        raise ValueError('任务日志身份发生变化，拒绝写入')
                    output.write(json.dumps({'time': _now(), **event}, ensure_ascii=False, default=str).encode('utf-8') + b'\n')
        except (OSError, ValueError) as error:
            if len(self._log_failures) < 4:
                self._log_failures.append('任务日志未能持续写入：' + str(error))

    def _emit(self, kind, **data):
        event = dict(type=kind, **data)
        if self._job:
            event.setdefault('job_dir', str(self._job))
        self._log_event(event)
        with self._lock:
            for key in ('stage', 'job_dir', 'can_resume', 'running'):
                if key in event:
                    self._status[key] = event[key]
            self.events.put(event)

    def _spawn(self, action):
        def worker():
            try:
                action()
            except Cancelled as error:
                try:
                    self._finish(dict(complete=False, cancelled=True, files=[], warnings=[str(error)], counts=reports.counts_for([])))
                except Exception as failure:
                    self._fail(failure)
            except Exception as error:
                self._fail(error)
        self._thread = threading.Thread(target=worker, name='usb-recovery-job', daemon=True)
        self._thread.start()

    def _setup_failed(self):
        with self._lock:
            self._status.update(running=False, stage='error')

    def start_image(self, device, destination):
        self._reserve()
        try:
            saved = dict(device)
            # Nothing may be created on the destination until it is proven to
            # be on a different physical disk. A rejected selection must not
            # even create our manifest or log on the source USB.
            self._preflight_capture(saved, destination, resume=False)
            job = self._new_job(destination, 'image', saved, {})
            self._spawn(lambda: self._capture(saved, resume=False))
            return job
        except Exception:
            self._setup_failed(); raise

    def start_recovery(self, image, destination, map_path=None, deep=False, include_history=True, offset=None):
        self._reserve()
        try:
            if offset is not None and (type(offset) is not int or offset < 0):
                raise ValueError('分区偏移必须为非负整数')
            options = dict(image_path=str(Path(image).absolute()), map_path=str(Path(map_path).absolute()) if map_path else None,
                           deep=bool(deep), include_history=bool(include_history), offset=offset,
                           max_file_bytes=self.max_file_bytes, max_total_bytes=self.max_total_bytes,
                           reserve_bytes=self.reserve_bytes)
            job = self._new_job(destination, 'recovery', options['image_path'], options)
            self._spawn(lambda: self._recover(options))
            return job
        except Exception:
            self._setup_failed(); raise

    def cancel(self):
        # Serialize stop intent with terminal publication. A late stop must not
        # put a "cancelling" event after the terminal result and leave UI waiting.
        with self._lock:
            if not self._status['running']:
                return
            self._cancel.set()
            self._status['stage'] = 'cancelling'
            event = dict(type='state', stage='cancelling', job_dir=str(self._job or ''),
                         message='已请求安全停止；等待当前读取退出，已有结果会保留。')
            self._log_event(event)
            self.events.put(event)

    def resume(self, job_dir):
        job = _directory(job_dir)
        manifest_path = regular_file(job / 'manifest.json')
        if manifest_path.stat().st_size > MAX_MANIFEST_BYTES:
            raise ValueError('任务记录超过资源上限，无法安全续接')
        saved = json.loads(manifest_path.read_text(encoding='utf-8'))
        if not isinstance(saved, dict) or saved.get('schema') != 1 or not isinstance(saved.get('job_id'), str):
            raise ValueError('任务记录格式不支持')
        if saved.get('kind') != 'image':
            raise ValueError('镜像扫描尚不支持增量续扫；已保留部分结果，请作为新任务重新扫描')
        if not isinstance(saved.get('source'), dict):
            raise ValueError('任务记录缺少源设备身份')
        win.validate_device(saved['source'])
        self._reserve()
        try:
            # Existing records must remain byte-identical on preflight failure.
            self._preflight_capture(saved['source'], job, resume=True)
            self._job, self._manifest = job, saved
            self._manifest.update(job_dir=str(job), stage='starting', complete=False, cancelled=False)
            self._initialize_log()
            self._save_manifest()
            with self._lock:
                self._status['job_dir'] = str(job)
            self._spawn(lambda: self._capture(saved['source'], resume=True))
            return job
        except Exception:
            self._setup_failed(); raise

    def _capture(self, saved, resume):
        self._emit('state', stage='starting', message='正在重新核对USB身份、权限和输出物理磁盘。')
        check_cancel(self._cancel)
        current = self._preflight_capture(saved, self._job, resume=resume)
        if self._runner is None:
            from .acquisition import AcquisitionRunner
            self._runner = AcquisitionRunner(self.engine_dir)
        self._manifest['source'] = current
        self._manifest['can_resume'] = True
        self._save_manifest()
        self._emit('state', stage='imaging', can_resume=True, message='开始只读制作镜像；不会自动开始文件恢复。')
        def progress(event):
            event = dict(event)
            number = event.pop('percent', event.pop('progress', None))
            event.pop('type', None); event.pop('stage', None)
            self._emit('progress', stage='imaging', progress=number, **event)
        result = self._runner.run(current, self._job, size=current['size'],
                                  sector_size=current.get('logical_sector_size', 512),
                                  cancel=self._cancel, progress=progress, resume=resume)
        result = dict(result)
        result.update(kind='image', files=[], counts=reports.counts_for([]), warnings=result.get('warnings', []),
                      can_resume=bool(result.get('can_resume', False)), can_recover=False,
                      image_completeness='complete' if result.get('complete') else 'partial')
        image = result.get('image_path')
        if image:
            try:
                result['can_recover'] = regular_file(image).stat().st_size > 0
            except (OSError, ValueError):
                pass
        self._finish(result)

    def _preflight_capture(self, saved, destination, resume):
        """Read-only checks shared by pre-write and execution-time guards."""
        check_cancel(self._cancel)
        target = _directory(destination)
        current = win.matching_device(saved, win.enumerate_devices())
        remaining = current['size']
        if resume:
            existing = regular_file(target / 'source.img')
            remaining = max(0, remaining - existing.stat().st_size)
        win.guard_destination(target, current, remaining + self.reserve_bytes, app_path=Path(sys.executable))
        win.check_source_access(current)
        check_cancel(self._cancel)
        return current

    def _hash_entry(self, reader, entry):
        digest, prefix = hashlib.sha256(), b''
        for extent in entry['extents']:
            position, remaining = extent['offset'], extent['length']
            while remaining:
                check_cancel(self._cancel)
                data = reader.read(position, min(1024 * 1024, remaining))
                if len(prefix) < 8:
                    prefix += data[:8 - len(prefix)]
                digest.update(data)
                position += len(data); remaining -= len(data)
        return digest.hexdigest(), prefix

    def _recover(self, options):
        result = dict(kind='recovery', files=[], warnings=[], counts=reports.counts_for([]), complete=True,
                      cancelled=False, can_resume=False, image_completeness='unknown',
                      image_path=options['image_path'], map_path=options['map_path'], options=options,
                      can_recover=False, directories=[])
        hashes = {}
        try:
            with ImageReader(options['image_path'], map_path=options['map_path'], cancel=self._cancel) as whole:
                truncated_file = whole.map is not None and whole.map.end > whole.size
                result['source_identity'] = dict(size=whole.size, device=whole._identity[0], inode=whole._identity[1],
                                                mtime_ns=os.fstat(whole._handle.fileno()).st_mtime_ns,
                                                map_sha256=whole.map_sha256)
                result['image_completeness'] = ('unknown' if whole.map is None else
                                                'complete' if whole.map.permits(0, whole.size) else 'partial')
                volumes = detect_volumes(whole)
                result['volumes'] = volumes
                offset = options['offset']
                if offset is None:
                    if len(volumes) != 1:
                        raise ValueError('镜像包含多个卷，请先识别并明确选择分区')
                    volume = volumes[0]
                else:
                    matches = [v for v in volumes if v['offset'] == offset]
                    if len(matches) != 1:
                        raise ValueError('所选分区偏移未能与镜像中的唯一卷匹配')
                    volume = matches[0]
                self._manifest['source_identity'] = result['source_identity']
                self._manifest['selected_volume'] = volume
                self._save_manifest()
                with ImageReader(options['image_path'], map_path=options['map_path'], offset=volume['offset'],
                                 length=volume['size'], cancel=self._cancel) as reader:
                    if reader._identity != whole._identity or reader.map_sha256 != whole.map_sha256:
                        raise ValueError('准备期间源镜像或读取记录被替换，拒绝继续')
                    result['image_completeness'] = ('unknown' if reader.map is None else
                                                    'complete' if reader.map.permits(reader.base_offset, reader.size) else 'partial')
                    if truncated_file:
                        result['image_completeness']='partial'
                        result['warnings'].append('读取记录容量大于已保存镜像文件；只处理已保存且确认可读的范围，镜像整体仍不完整。')
                    result['warnings'].extend(reader.warnings)
                    result['warnings'].extend(volume.get('warnings', []))
                    limit = self.max_total_bytes if self.max_total_bytes is not None else min(max(reader.size * 2, 1), 2 * 1024**4)
                    budget = _OutputBudget(self._job, limit, self.max_file_bytes, self.reserve_bytes)
                    result['output_limits'] = dict(total_bytes=limit, per_file_bytes=self.max_file_bytes, reserve_bytes=self.reserve_bytes)
                    if volume['filesystem'].casefold() == 'exfat':
                        try:
                            self._recover_directory(reader, result, hashes, budget, options)
                        except Cancelled:
                            raise
                        except Exception as error:
                            result['complete'] = False
                            result['warnings'].append('exFAT目录处理未完成：' + str(error))
                    else:
                        result['warnings'].append('所选卷非已识别exFAT：本版不支持此卷目录恢复。')
                        if not options['deep']:
                            result['complete'] = False
                    if options['deep']:
                        check_cancel(self._cancel)
                        self._emit('state', stage='deep_scan', message='正在深搜Office候选；原名和历史版本无法据此确认。')
                        def scan_progress(event):
                            self._emit('progress', stage='deep_scan', progress=event.get('percent'),
                                       message=event.get('message', '正在深搜'), counts=reports.counts_for(result['files']))
                        scan = deep_scan.scan_office(reader, self._new_subdir('deep_scan'), known_hashes=set(hashes),
                                                     progress=scan_progress, cancel=self._cancel, output_guard=budget.guard)
                        result['deep_scan'] = {key: value for key, value in scan.items() if key != 'files'}
                        result['warnings'].extend(scan['warnings'])
                        result['complete'] = result['complete'] and bool(scan['complete'])
                        result['cancelled'] = bool(scan['cancelled'])
                        for item in scan['files']:
                            record = dict(item, output_path=str(item['path']), validation_status=item.get('status', 'unverified'),
                                          original_path=f"Office字节候选@{reader.base_offset + item['offset']}",
                                          physical_offset=reader.base_offset + item['offset'])
                            result['files'].append(record)
                            hashes[item['sha256']] = record
                    result['output_reserved_bytes'] = budget.used
                    check_cancel(self._cancel)
                    after = os.fstat(reader._handle.fileno())
                    stable = after.st_size == whole.size and after.st_mtime_ns == result['source_identity']['mtime_ns']
                    result['source_identity_stable'] = stable
                    result['source_identity_check'] = '同一打开文件身份、长度及修改时间；不是源镜像全量hash验证'
                    if not stable:
                        result['complete'] = False
                        result['warnings'].append('处理期间源镜像大小或修改时间发生变化，全部候选需要额外复核。')
        except Cancelled as error:
            result.update(complete=False, cancelled=True)
            result['warnings'].append(str(error))
        except Exception as error:
            result['complete'] = False
            result['warnings'].append(f'{type(error).__name__}: {error}')
        if result['image_completeness'] == 'partial':
            result['complete'] = False
        if result['cancelled']:
            result['warnings'].append('已保留部分结果；镜像扫描尚不支持增量续扫，继续时请创建新任务重新扫描。')
        self._finish(result)

    def _new_subdir(self, name):
        with exfat._output_tree(self._job, []):
            path = self._job / name
            path.mkdir(exist_ok=False)
        return path

    def _recover_directory(self, reader, result, hashes, budget, options):
        self._emit('state', stage='inventory', message='正在只读识别exFAT目录与候选范围。')
        data = exfat.inventory(reader, include_deleted=options['include_history'])
        result['geometry'] = data['geometry']
        result['directories'] = data['directories']
        result['warnings'].extend(data['warnings'])
        result['directory_scope_complete']=bool(data.get('complete',True))
        if not data.get('complete',True) or data.get('resource_limited',False):
            result['complete']=False
        if not data['geometry'].get('verified', False):
            result['complete'] = False
        if any(not item.get('readable', True) or item.get('anomalies') for item in data['directories']):
            result['complete'] = False
        entries = [item for item in data['files'] if options['include_history'] or item.get('origin') == 'current']
        if len(entries) > MAX_RECORDS:
            result['complete'] = False
            result['warnings'].append(f'文件条目超过本次 {MAX_RECORDS} 项处理上限，其余条目未输出。')
            entries = entries[:MAX_RECORDS]
        if any('limit' in str(w).lower() or 'not traversed' in str(w).lower() for w in data['warnings']):
            result['complete'] = False
        output_root = self._new_subdir('files')
        repairs_root = self._new_subdir('repairs')
        for index, entry in enumerate(entries):
            check_cancel(self._cancel)
            record = dict(id=entry.get('id', str(index)), original_path=entry['path'], origin=entry.get('origin', 'historical'),
                          size=entry['size'], extents=entry.get('extents', []), warnings=list(entry.get('warnings', [])),
                          validation_status='unverified', status='unverified')
            result['files'].append(record)
            try:
                if not entry.get('readable'):
                    raise ValueError('文件范围不可完整读取，未输出缺块或未初始化数据')
                size = entry['size']
                if type(size) is not int or size < 0 or size > self.max_file_bytes or size > reader.size:
                    raise ValueError('文件大小超过安全上限或镜像范围，未输出')
                budget.guard(output_root / 'pending-candidate', size, reserve=False)
                digest, prefix = self._hash_entry(reader, entry)
                record['sha256'] = digest
                if digest in hashes:
                    original = hashes[digest]
                    record.update(duplicate_of=original.get('output_path', original.get('path', original.get('id', digest))),
                                  validation_status=original.get('validation_status', 'unverified'))
                    original.setdefault('aliases', []).append(dict(path=entry['path'], origin=record['origin']))
                    continue
                budget.guard(output_root / 'pending-candidate', size)
                extracted = exfat.extract_file(reader, entry, output_root)
                record.update(output_path=str(extracted['path']), bytes=extracted['bytes'], extracted_sha256=extracted['sha256'])
                if extracted['sha256'] != digest or extracted['bytes'] != size:
                    raise ValueError('提取后字节或hash与预读不符，文件只作为未确认候选保留')
                record['warnings'] = list(dict.fromkeys(record['warnings'] + extracted.get('warnings', [])))
                name = Path(extracted['path']).name
                is_system = name.startswith('~$') or any(part.casefold() in ('$recycle.bin', 'system volume information', '.trashes')
                                                                    for part in entry.get('components', []))
                if is_system:
                    record['validation_status'] = 'temporary'
                elif Path(name).suffix.lower() in OFFICE_SUFFIXES or prefix.startswith((b'PK\x03\x04', office.OLE_MAGIC)):
                    validation = office.inspect_document(extracted['path'])
                    record.update(validation_status=validation['status'], validation=validation)
                    record['warnings'].extend(validation.get('warnings', []))
                    if validation['status'] not in ('structure_checked', 'temporary') and size <= office.MAX_SOURCE_BYTES:
                        repair_dir = repairs_root / ('file_' + str(index))
                        with exfat._output_tree(repairs_root, []):
                            repair_dir.mkdir(exist_ok=False)
                        salvage = office.salvage_document(extracted['path'], repair_dir, cancel=self._cancel, output_guard=budget.guard)
                        record['salvage'] = salvage
                        if salvage.get('output_limited') or salvage.get('cancelled'):
                            result['complete'] = False
                        for candidate in salvage.get('candidates', []):
                            candidate_hash = candidate['sha256']
                            repaired = dict(candidate, output_path=str(candidate['path']), validation_status='repaired_candidate',
                                            origin=record['origin'], original_path=record['original_path'],
                                            bytes=Path(candidate['path']).stat().st_size, source_record_id=record['id'],
                                            warnings=['容器修复候选，须独立核对；不替代原提取件。'])
                            if candidate_hash in hashes:
                                repaired['duplicate_of'] = hashes[candidate_hash].get('output_path', candidate_hash)
                            else:
                                hashes[candidate_hash] = repaired
                            result['files'].append(repaired)
                        check_cancel(self._cancel)
                else:
                    record['warnings'].append('通用文件仅提取字节，未实施该格式的内容完整性检查。')
                record['status'] = record['validation_status']
                hashes[digest] = record
            except Cancelled:
                record.update(validation_status='partial', status='partial')
                record['warnings'].append('提取被停止，可能保留明确标记的.partial文件。')
                record['partial_outputs'] = self._partial_files(output_root)
                raise
            except Exception as error:
                result['complete'] = False
                record.update(validation_status='partial', status='partial')
                reason = str(error)
                record['warnings'].append(reason)
                result['warnings'].append(f"{entry['path']}：{reason}")
                record['partial_outputs'] = self._partial_files(output_root)
            self._emit('progress', stage='recovering', progress=(index + 1) * 100 / max(1, len(entries)),
                       message=f'已处理目录条目 {index + 1}/{len(entries)}；分类结果仍需核对。',
                       counts=reports.counts_for(result['files']))

    def _partial_files(self, root):
        found = []
        for parent, directories, names in os.walk(root, followlinks=False):
            try:
                no_reparse(Path(parent))
            except ValueError:
                directories[:] = []
                continue
            for name in names:
                if name.endswith('.partial'):
                    try:
                        path = regular_file(Path(parent) / name)
                        found.append(dict(path=str(path), bytes=path.stat().st_size, status='partial'))
                    except (OSError, ValueError):
                        continue
                    if len(found) >= 1000:
                        return found
        return found

    def _finish(self, result):
        result.setdefault('files', [])
        result.setdefault('warnings', [])
        result['warnings'].extend(self._log_failures)
        result['counts'] = reports.counts_for(result['files'])
        result.update(job_dir=str(self._job), finished_at=_now())
        result.setdefault('can_resume', False)
        result.setdefault('complete', False)
        result.setdefault('cancelled', False)
        if self._cancel.is_set():
            result.update(complete=False, cancelled=True)
        # Keep evidence if a later report/manifest write fails.
        self.last_result = result
        paths = reports.write_reports(self._job, result)
        result.update(paths)
        stage = 'cancelled' if result['cancelled'] else 'completed'
        self._manifest.update(stage=stage, complete=bool(result['complete']), cancelled=bool(result['cancelled']),
                              can_resume=bool(result['can_resume']), counts=result['counts'],
                              report_path=result['report_path'], result_json=result['json_path'])
        if result.get('image_path'):
            self._manifest.update(image_path=result['image_path'], map_path=result.get('map_path'))
        self._save_manifest()
        self.last_result = result
        message = ('已安全停止，部分结果保留。' if result['cancelled'] else
                   '本次处理已结束，请查看报告核对文件。' if result['complete'] else '部分结果已保留；报告列明未完成范围及文件状态。')
        self._emit('result', stage=stage, running=False, complete=bool(result['complete']), cancelled=bool(result['cancelled']),
                   message=message, counts=result['counts'], report_path=result['report_path'],
                   can_resume=bool(result['can_resume']), can_recover=bool(result.get('can_recover', False)),
                   image_path=result.get('image_path'), map_path=result.get('map_path'), progress=None)

    def _fail(self, error):
        result = dict(self.last_result or {})
        result.update(complete=False, cancelled=self._cancel.is_set(), can_resume=False, job_dir=str(self._job))
        result.setdefault('files', [])
        result['warnings'] = list(result.get('warnings', [])) + [f'{type(error).__name__}: {error}']
        result['counts'] = reports.counts_for(result['files'])
        if self._manifest and self._manifest.get('kind') == 'image':
            try:
                from .acquisition import resume_available
                result['can_resume'] = resume_available(self._job)
            except (OSError, ValueError):
                pass
        try:
            if self._job:
                result.update(reports.write_reports(self._job, result))
                self._manifest.update(stage='error', complete=False, cancelled=result['cancelled'],
                                      error=str(error), can_resume=result['can_resume'], report_path=result.get('report_path'))
                self._save_manifest()
        except Exception as report_error:
            result['warnings'].append('任务报告或状态记录未能完全保存：' + str(report_error))
        self.last_result = result
        self._emit('error', stage='error', running=False, complete=False, cancelled=result['cancelled'],
                   message='；'.join(result['warnings']), can_resume=result['can_resume'],
                   report_path=result.get('report_path'), needs_admin=isinstance(error, win.AdminRequired))

    def restart_as_admin(self):
        if self.status()['running']:
            raise RuntimeError('请先停止当前任务，再以管理员身份重新打开')
        if os.name != 'nt':
            raise OSError('管理员启动仅适用于Windows')
        import ctypes
        from ctypes import wintypes
        if getattr(sys, 'frozen', False):
            executable, parameters = sys.executable, ''
        else:
            executable = sys.executable
            parameters = subprocess.list2cmdline(['-m', 'usb_recovery.app'])
        execute = ctypes.windll.shell32.ShellExecuteW
        execute.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR,
                            wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_int]
        execute.restype = wintypes.HINSTANCE
        result = execute(None, 'runas', executable, parameters, None, 1) or 0
        if result <= 32:
            raise OSError('Windows未启动管理员窗口；可能取消了授权。当前任务记录仍保留。')
        return True


def run_recovery_sync(image, destination, map_path=None, deep=False, include_history=True, offset=None, **limits):
    """CLI/self-check helper; GUI must use Controller.start_recovery instead."""
    controller = Controller(**limits)
    controller.start_recovery(image, destination, map_path=map_path, deep=deep,
                              include_history=include_history, offset=offset)
    controller._thread.join()
    return controller.last_result
