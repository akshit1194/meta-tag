"""Bounded isolated workers. No upload names or metadata ever enter a shell."""
import json
import os
import signal
import subprocess
import sys
from pathlib import Path


class WorkerError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def windows_job(process, memory_mb):
    """Apply a job-wide memory cap, including ExifTool descendants, before input."""
    import ctypes
    from ctypes import wintypes

    class BASIC(ctypes.Structure):
        _fields_ = [('PerProcessUserTimeLimit', ctypes.c_int64), ('PerJobUserTimeLimit', ctypes.c_int64),
                    ('LimitFlags', wintypes.DWORD), ('MinimumWorkingSetSize', ctypes.c_size_t),
                    ('MaximumWorkingSetSize', ctypes.c_size_t), ('ActiveProcessLimit', wintypes.DWORD),
                    ('Affinity', ctypes.c_size_t), ('PriorityClass', wintypes.DWORD), ('SchedulingClass', wintypes.DWORD)]

    class IO(ctypes.Structure):
        _fields_ = [(n, ctypes.c_uint64) for n in ('ReadOperationCount', 'WriteOperationCount',
                    'OtherOperationCount', 'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount')]

    class EXTENDED(ctypes.Structure):
        _fields_ = [('BasicLimitInformation', BASIC), ('IoInfo', IO), ('ProcessMemoryLimit', ctypes.c_size_t),
                    ('JobMemoryLimit', ctypes.c_size_t), ('PeakProcessMemoryUsed', ctypes.c_size_t),
                    ('PeakJobMemoryUsed', ctypes.c_size_t)]

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    job = kernel.CreateJobObjectW(None, None)
    if not job:
        raise WorkerError('RESOURCE_LIMIT_SETUP_FAILED')
    info = EXTENDED()
    info.BasicLimitInformation.LimitFlags = 0x2000 | 0x200  # KILL_ON_JOB_CLOSE | JOB_MEMORY
    info.JobMemoryLimit = memory_mb * 1024 * 1024
    if not kernel.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)) or not kernel.AssignProcessToJobObject(job, wintypes.HANDLE(process._handle)):
        kernel.CloseHandle(job)
        raise WorkerError('RESOURCE_LIMIT_SETUP_FAILED')
    return lambda: kernel.CloseHandle(job)


def run_worker(payload, *, timeout=12, memory_mb=768):
    payload = dict(payload, memory_mb=memory_mb, cpu_seconds=max(1, int(timeout)))
    project = Path(__file__).resolve().parent.parent
    try:
        process = subprocess.Popen(
            [sys.executable, '-m', 'photo_app.worker'], cwd=project,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            start_new_session=os.name != 'nt',
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
        )
    except OSError:
        raise WorkerError('WORKER_START_FAILED') from None
    close_job = None
    try:
        if os.name == 'nt':
            close_job = windows_job(process, memory_mb)
        # The worker blocks on stdin until the Windows job has been attached.
        output, _ = process.communicate(json.dumps(payload).encode(), timeout=timeout)
        if process.returncode != 0:
            raise WorkerError('WORKER_RESOURCE_LIMIT_OR_CRASH')
        if len(output) > 300_000:
            raise WorkerError('PARSER_OUTPUT_LIMIT')
        try:
            result = json.loads(output)
        except (ValueError, UnicodeError):
            raise WorkerError('INVALID_WORKER_RESPONSE') from None
        if not isinstance(result, dict):
            raise WorkerError('INVALID_WORKER_RESPONSE')
        if result.get('error'):
            raise WorkerError(result['error'])
        return result
    except subprocess.TimeoutExpired:
        raise WorkerError('WORKER_TIMEOUT') from None
    finally:
        if close_job:
            close_job()  # Kills ExifTool and any remaining child processes as well.
        elif os.name != 'nt':
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if process.poll() is None:
            process.kill()
        process.communicate()
