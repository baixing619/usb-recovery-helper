"""Windows device discovery and destination guards. No write-capable device handles."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess


class SafetyError(ValueError):
    pass


class AdminRequired(PermissionError):
    pass


def validate_device(device: dict) -> dict:
    if str(device.get('bus_type', '')).upper() != 'USB':
        raise SafetyError('首版只允许采集已识别为USB的设备。')
    if device.get('is_boot') is not False or device.get('is_system') is not False:
        raise SafetyError('不能采集系统盘或启动盘。')
    for key in ('disk_number', 'size', 'disk_size', 'offset'):
        value = device.get(key)
        if type(value) is not int or value < (1 if key in ('size', 'disk_size') else 0):
            raise SafetyError('设备容量或位置不完整，不能开始读取。')
    if device['disk_number'] > 4095 or device['size'] > 2**60 or device['offset'] + device['size'] > device['disk_size']:
        raise SafetyError('设备容量或位置不合法。')
    if not isinstance(device.get('unique_id'), str) or not device['unique_id'].strip():
        raise SafetyError('无法取得可靠设备身份，不能安全续接或采集。')
    if device.get('logical_sector_size', 512) not in (512, 1024, 2048, 4096):
        raise SafetyError('不支持此设备的逻辑扇区大小。')
    kind = device.get('source_kind')
    if kind not in ('physical', 'volume'):
        raise SafetyError('未知设备类型。')
    if kind == 'volume':
        if type(device.get('partition_number')) is not int or not 1 <= device['partition_number'] <= 256:
            raise SafetyError('分区编号不合法。')
        letter = device.get('drive_letter')
        if not isinstance(letter, str) or not re.fullmatch('[A-Za-z]', letter):
            raise SafetyError('此分区没有可读取的盘符，请选择整盘保全。')
    else:
        if device['offset'] != 0 or device['size'] != device['disk_size']:
            raise SafetyError('整盘范围与设备容量不一致。')
    return device


def device_fingerprint(device: dict) -> str:
    validate_device(device)
    keys = ('disk_number', 'partition_number', 'source_kind', 'size', 'disk_size', 'offset', 'logical_sector_size')
    identity = {key: device.get(key, 512 if key == 'logical_sector_size' else None) for key in keys}
    for key in ('unique_id', 'serial', 'model'):
        identity[key] = str(device.get(key) or '').strip().casefold()
    return hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()


def matching_device(saved: dict, devices: list[dict]) -> dict:
    expected = device_fingerprint(saved)
    matches = []
    for item in devices:
        try:
            if device_fingerprint(item) == expected:
                matches.append(item)
        except SafetyError:
            continue
    if len(matches) != 1:
        raise SafetyError('设备身份、容量或位置发生变化，无法安全续接；原镜像已保留。请重新检查所选U盘。')
    return matches[0]


def _disk_letters(number: int) -> str:
    result, number = '', number + 1
    while number:
        number, remainder = divmod(number - 1, 26)
        result = chr(97 + remainder) + result
    return result


def cygwin_device_path(device: dict) -> str:
    validate_device(device)
    suffix = str(device['partition_number']) if device['source_kind'] == 'volume' else ''
    return '/dev/sd' + _disk_letters(device['disk_number']) + suffix


def windows_device_path(device: dict) -> str:
    validate_device(device)
    if device['source_kind'] == 'volume':
        return '\\\\.\\' + device['drive_letter'].upper() + ':'
    return '\\\\.\\PhysicalDrive' + str(device['disk_number'])


def _kernel():
    if os.name != 'nt':
        raise SafetyError('此功能需要Windows 10/11。')
    dll = ctypes.WinDLL('kernel32', use_last_error=True)
    dll.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    dll.CreateFileW.restype = wintypes.HANDLE
    dll.CloseHandle.argtypes = [wintypes.HANDLE]
    dll.CloseHandle.restype = wintypes.BOOL
    return dll


def _open_handle(path: str, access: int = 0, flags: int = 0):
    dll = _kernel()
    handle = dll.CreateFileW(path, access, 1 | 2 | 4, None, 3, flags, None)
    if handle == ctypes.c_void_p(-1).value:
        error = ctypes.get_last_error()
        if error == 5:
            raise AdminRequired('当前权限无法读取此设备。可选择带盘符的分区，或点击“以管理员身份重启”。')
        raise OSError(error, ctypes.FormatError(error), path)
    return dll, handle


def check_source_access(device: dict) -> None:
    """Open-only permission check; GENERIC_READ, OPEN_EXISTING, never a write bit."""
    dll, handle = _open_handle(windows_device_path(device), 0x80000000)
    dll.CloseHandle(handle)


def read_source_prefix(device: dict, length: int) -> bytes:
    """Small, sector-aligned resume identity check; never a write handle."""
    sector = device.get('logical_sector_size', 512)
    if length != sector or length not in (512, 1024, 2048, 4096):
        raise SafetyError('续接指纹读取范围不合法。')
    dll, handle = _open_handle(windows_device_path(device), 0x80000000)
    try:
        dll.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                                 ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
        dll.ReadFile.restype = wintypes.BOOL
        data, count = ctypes.create_string_buffer(length), wintypes.DWORD()
        if not dll.ReadFile(handle, data, length, ctypes.byref(count), None) or count.value != length:
            raise SafetyError('无法核对原U盘的续接指纹，已保留镜像，未继续读取。')
        return data.raw
    finally:
        dll.CloseHandle(handle)


def is_admin() -> bool:
    if os.name != 'nt':
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except OSError:
        return False


def powershell_path() -> Path:
    dll = _kernel()
    buffer = ctypes.create_unicode_buffer(32768)
    dll.GetWindowsDirectoryW.argtypes = [wintypes.LPWSTR, wintypes.UINT]
    dll.GetWindowsDirectoryW.restype = wintypes.UINT
    if not dll.GetWindowsDirectoryW(buffer, len(buffer)):
        raise ctypes.WinError(ctypes.get_last_error())
    return Path(buffer.value) / 'System32' / 'WindowsPowerShell' / 'v1.0' / 'powershell.exe'


_DISCOVERY_SCRIPT = r"""
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$items = @(foreach ($d in @(Get-Disk | Where-Object { $_.BusType -eq 'USB' })) {
  $uid = [string]$d.UniqueId
  if ([string]::IsNullOrWhiteSpace($uid)) { $uid = [string]$d.Path }
  $common = @{ disk_number=[int]$d.Number; disk_size=[long]$d.Size; unique_id=$uid.Trim();
    serial=([string]$d.SerialNumber).Trim(); model=[string]$d.FriendlyName; bus_type='USB';
    is_boot=[bool]$d.IsBoot; is_system=[bool]$d.IsSystem; logical_sector_size=[int]$d.LogicalSectorSize }
  $p = $common.Clone(); $p.id='disk-'+$d.Number+'-all'; $p.source_kind='physical';
  $p.partition_number=$null; $p.drive_letter=$null; $p.offset=[long]0; $p.size=[long]$d.Size;
  $p.label=([string]$d.FriendlyName)+' | '+[math]::Round($d.Size/1GB,1)+' GiB | 整盘';
  [pscustomobject]$p
  foreach ($part in @(Get-Partition -DiskNumber $d.Number -ErrorAction SilentlyContinue)) {
    if ($part.DriveLetter -match '^[A-Za-z]$') {
      $v=$common.Clone(); $v.id='disk-'+$d.Number+'-part-'+$part.PartitionNumber;
      $v.source_kind='volume'; $v.partition_number=[int]$part.PartitionNumber;
      $v.drive_letter=[string]$part.DriveLetter; $v.offset=[long]$part.Offset; $v.size=[long]$part.Size;
      $v.label=([string]$part.DriveLetter)+': | '+[string]$d.FriendlyName+' | '+[math]::Round($part.Size/1GB,1)+' GiB | 分区';
      [pscustomobject]$v
    }
  }
})
ConvertTo-Json -InputObject $items -Depth 4 -Compress
"""


def enumerate_devices() -> list[dict]:
    process = subprocess.run([str(powershell_path()), '-NoLogo', '-NoProfile', '-NonInteractive',
                              '-Command', _DISCOVERY_SCRIPT], stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
                             creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if process.returncode:
        raise SafetyError('Windows未能列出USB设备；请检查设备连接和磁盘管理服务。')
    values = json.loads(process.stdout.decode('utf-8-sig'))
    if not isinstance(values, list):
        raise SafetyError('Windows设备列表格式异常，未开始读取任何设备。')
    result = []
    for item in values:
        try:
            validate_device(item)
            result.append(item)
        except SafetyError:
            pass
    return result


def path_has_reparse(path: Path) -> bool:
    absolute = Path(os.path.abspath(path))
    for part in (absolute, *absolute.parents):
        st = part.lstat()
        if part.is_symlink() or getattr(st, 'st_file_attributes', 0) & 0x400:
            return True
    return False


def storage_info(path: Path) -> dict:
    path = Path(os.path.abspath(path))
    if not path.is_dir():
        raise SafetyError('请选择一个已经存在的本地文件夹。')
    if path_has_reparse(path):
        raise SafetyError('此文件夹含链接或挂载点，请选择磁盘上的普通文件夹。')
    dll, handle = _open_handle(str(path), 0, 0x02000000)
    try:
        dll.GetFinalPathNameByHandleW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
        dll.GetFinalPathNameByHandleW.restype = wintypes.DWORD
        final = ctypes.create_unicode_buffer(32768)
        size = dll.GetFinalPathNameByHandleW(handle, final, len(final), 0)
        if not 0 < size < len(final):
            raise ctypes.WinError(ctypes.get_last_error())
        canonical = final.value
    finally:
        dll.CloseHandle(handle)
    if canonical.startswith('\\\\?\\'):
        canonical = canonical[4:]
    if not re.match(r'^[A-Za-z]:\\', canonical):
        raise SafetyError('首版需将镜像保存到可确认身份的本地磁盘。')
    root = ctypes.create_unicode_buffer(32768)
    dll.GetVolumePathNameW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    dll.GetVolumePathNameW.restype = wintypes.BOOL
    if not dll.GetVolumePathNameW(canonical, root, len(root)):
        raise ctypes.WinError(ctypes.get_last_error())
    dll.GetVolumeNameForVolumeMountPointW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    dll.GetVolumeNameForVolumeMountPointW.restype = wintypes.BOOL
    guid = ctypes.create_unicode_buffer(1024)
    if not dll.GetVolumeNameForVolumeMountPointW(root.value, guid, len(guid)):
        raise ctypes.WinError(ctypes.get_last_error())
    fs = ctypes.create_unicode_buffer(64)
    dll.GetVolumeInformationW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD,
                                         ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
                                         ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR, wintypes.DWORD]
    dll.GetVolumeInformationW.restype = wintypes.BOOL
    serial, max_component, flags = wintypes.DWORD(), wintypes.DWORD(), wintypes.DWORD()
    if not dll.GetVolumeInformationW(root.value, None, 0, ctypes.byref(serial), ctypes.byref(max_component),
                                     ctypes.byref(flags), fs, len(fs)):
        raise ctypes.WinError(ctypes.get_last_error())
    _, volume = _open_handle(guid.value.rstrip('\\'), 0)
    try:
        dll.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                        ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
        dll.DeviceIoControl.restype = wintypes.BOOL
        output = ctypes.create_string_buffer(65536)
        returned = wintypes.DWORD()
        if not dll.DeviceIoControl(volume, 0x00560000, None, 0, output, len(output), ctypes.byref(returned), None):
            raise ctypes.WinError(ctypes.get_last_error())
        count = int.from_bytes(output.raw[:4], 'little')
        if not 1 <= count <= 256 or returned.value < 8 + count * 24:
            raise SafetyError('目标卷的物理磁盘信息不完整。')
        numbers = sorted({int.from_bytes(output.raw[8+i*24:12+i*24], 'little') for i in range(count)})
    finally:
        dll.CloseHandle(volume)
    return dict(canonical_path=canonical, drive_root=root.value, disk_numbers=numbers,
                filesystem=fs.value, free_bytes=shutil.disk_usage(canonical).free, has_reparse=False)


def guard_destination(destination: Path, source: dict, required_bytes: int,
                      inspector=storage_info, app_path: Path | None = None) -> dict:
    validate_device(source)
    info = inspector(Path(destination))
    disks = info.get('disk_numbers')
    if info.get('has_reparse') or not isinstance(disks, list) or not disks or any(type(n) is not int or n < 0 for n in disks):
        raise SafetyError('无法确认目标文件夹所属的物理磁盘，未开始采集。')
    if source['disk_number'] in disks:
        raise SafetyError('目标文件夹与要恢复的U盘在同一块物理磁盘，请选择电脑硬盘或另一块磁盘。')
    if required_bytes < 0 or info.get('free_bytes', -1) < required_bytes:
        raise SafetyError('目标盘剩余空间不足，无法安全保存此镜像。')
    fs = str(info.get('filesystem', '')).casefold()
    if fs == 'fat32' and required_bytes > 2**32 - 1:
        raise SafetyError('所选目标盘单个文件上限约4GB，请选择能保存大镜像的其他磁盘。')
    if fs not in ('ntfs', 'exfat', 'refs', 'fat32'):
        raise SafetyError('此目标文件系统尚不支持镜像保存，请选择本地NTFS或exFAT磁盘。')
    if app_path is not None:
        app_info = inspector(Path(app_path).parent)
        if source['disk_number'] in app_info.get('disk_numbers', []):
            raise SafetyError('软件正从待恢复U盘运行。请先把软件复制到电脑或另一块磁盘。')
    return info
