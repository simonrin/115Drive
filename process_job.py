"""Own a transfer process tree, including when its Python supervisor is killed."""
import contextlib
import ctypes as c
import os

class BasicLimit(c.Structure):
    _fields_=[('process_time',c.c_int64),('job_time',c.c_int64),('flags',c.c_uint32),
              ('minimum',c.c_size_t),('maximum',c.c_size_t),('active',c.c_uint32),
              ('affinity',c.c_size_t),('priority',c.c_uint32),('scheduling',c.c_uint32)]
class ExtendedLimit(c.Structure):
    _fields_=[('basic',BasicLimit),('io',c.c_uint64*6),('process_memory',c.c_size_t),
              ('job_memory',c.c_size_t),('peak_process_memory',c.c_size_t),('peak_job_memory',c.c_size_t)]

@contextlib.contextmanager
def owned_process(process):
    if os.name!='nt':
        yield process;return
    kernel=c.WinDLL('kernel32',use_last_error=True)
    kernel.CreateJobObjectW.argtypes=[c.c_void_p,c.c_wchar_p];kernel.CreateJobObjectW.restype=c.c_void_p
    kernel.SetInformationJobObject.argtypes=[c.c_void_p,c.c_int,c.c_void_p,c.c_uint32]
    kernel.AssignProcessToJobObject.argtypes=[c.c_void_p,c.c_void_p]
    kernel.CloseHandle.argtypes=[c.c_void_p]
    handle=kernel.CreateJobObjectW(None,None)
    try:
        limits=ExtendedLimit();limits.basic.flags=0x2000
        if not handle or not kernel.SetInformationJobObject(handle,9,c.byref(limits),c.sizeof(limits)) or not kernel.AssignProcessToJobObject(handle,int(process._handle)):
            if process.poll() is None:process.terminate();process.wait(timeout=15)
            raise c.WinError(c.get_last_error())
        yield process
    finally:
        if handle:kernel.CloseHandle(handle)
