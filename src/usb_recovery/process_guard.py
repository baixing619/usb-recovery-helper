"""Contain an imaging child so a crashed parent cannot leave it reading a device."""
import ctypes
from ctypes import wintypes
import os


class ProcessGuard:
    def __init__(self):
        self.handle = None
        self.kernel = None
        if os.name != 'nt':
            return
        size_t = ctypes.c_size_t
        class Basic(ctypes.Structure):
            _fields_ = [('per_process_time', ctypes.c_longlong), ('per_job_time', ctypes.c_longlong),
                        ('flags', wintypes.DWORD), ('min_working', size_t), ('max_working', size_t),
                        ('process_limit', wintypes.DWORD), ('affinity', size_t),
                        ('priority', wintypes.DWORD), ('scheduling', wintypes.DWORD)]
        class Counters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in ('read_ops','write_ops','other_ops','read_bytes','write_bytes','other_bytes')]
        class Extended(ctypes.Structure):
            _fields_ = [('basic', Basic), ('io', Counters), ('process_memory', size_t),
                        ('job_memory', size_t), ('peak_process', size_t), ('peak_job', size_t)]
        dll = ctypes.WinDLL('kernel32', use_last_error=True)
        dll.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        dll.CreateJobObjectW.restype = wintypes.HANDLE
        dll.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        dll.SetInformationJobObject.restype = wintypes.BOOL
        dll.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        dll.AssignProcessToJobObject.restype = wintypes.BOOL
        dll.CloseHandle.argtypes = [wintypes.HANDLE]
        dll.CloseHandle.restype = wintypes.BOOL
        handle = dll.CreateJobObjectW(None, None)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = Extended()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not dll.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.get_last_error(); dll.CloseHandle(handle)
            raise ctypes.WinError(error)
        self.handle, self.kernel = handle, dll

    def assign(self, process):
        if self.handle is not None and not self.kernel.AssignProcessToJobObject(self.handle, int(process._handle)):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self):
        if self.handle is not None:
            self.kernel.CloseHandle(self.handle)
            self.handle = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
