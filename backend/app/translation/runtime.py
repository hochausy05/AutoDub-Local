"""Owned llama-server lifecycle for the configured Windows installation."""
import ctypes
import os
import secrets
import socket
import subprocess
import time
import hashlib
import httpx
from pathlib import Path

RUNTIME = Path(os.environ.get("AUTODUB_LOCAL_LLAMA_PATH", r"D:\LocalAI\llama\llama-server.exe"))
MODEL = Path(os.environ.get("AUTODUB_LOCAL_MODEL_PATH", r"D:\LocalAI\models\Hy-MT3-7B-Q4_K_M.gguf"))
try:
    PORT = int(os.environ.get("AUTODUB_LOCAL_PORT", "8080"))
    PORT_VALID = 1 <= PORT <= 65535
except ValueError:
    PORT, PORT_VALID = 0, False

class RuntimeErrorCode(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)

def _supported_runtime_version(returncode: int, stdout: str, stderr: str) -> bool:
    return returncode == 0 and "build 12676" in f"{stdout}\n{stderr}"

class RuntimeManager:
    def __init__(self, runtime: Path = RUNTIME, model: Path = MODEL, port: int = PORT):
        self.runtime, self.model, self.port = runtime, model, port
        self.process = None
        self.thread_handle = None
        self.process_handle = None
        self.job_handle = None
        self.api_key = None

    @staticmethod
    def _sha256(path: Path) -> str:
        digest=hashlib.sha256()
        with path.open("rb") as stream:
            while chunk:=stream.read(1024*1024): digest.update(chunk)
        return digest.hexdigest()

    def start(self, *, expected_model_sha256: str | None = None, expected_runtime_identity: str | None = None):
        if os.name != "nt":
            raise RuntimeErrorCode("local_runtime_windows_required")
        if not self.runtime.is_absolute() or not self.runtime.is_file():
            raise RuntimeErrorCode("local_runtime_missing")
        if not self.model.is_absolute() or not self.model.is_file():
            raise RuntimeErrorCode("local_model_missing")
        if not PORT_VALID or not 1 <= self.port <= 65535:
            raise RuntimeErrorCode("local_runtime_config_invalid")
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", self.port)) == 0:
                raise RuntimeErrorCode("local_runtime_port_in_use")
        try:
            if expected_model_sha256 and self._sha256(self.model)!=expected_model_sha256:
                raise RuntimeErrorCode("local_model_identity_changed")
            if expected_runtime_identity and expected_runtime_identity!=f"llama.cpp/b12676:{self._sha256(self.runtime)}":
                raise RuntimeErrorCode("local_runtime_identity_changed")
        except OSError as exc:
            raise RuntimeErrorCode("local_model_or_runtime_missing") from exc
        try:
            version = subprocess.run([str(self.runtime), "--version"], stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10, check=False, text=True, encoding="utf-8", errors="replace")
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeErrorCode("local_runtime_identity_unavailable") from exc
        if not _supported_runtime_version(version.returncode, version.stdout, version.stderr):
            raise RuntimeErrorCode("local_runtime_version_mismatch")
        self.api_key = secrets.token_urlsafe(32)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel32.WaitForSingleObject.restype = ctypes.c_uint32
        kernel32.ResumeThread.argtypes = [ctypes.c_void_p]
        kernel32.ResumeThread.restype = ctypes.c_uint32
        kernel32.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel32.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        self.job_handle = kernel32.CreateJobObjectW(None, None)
        if not self.job_handle:
            raise RuntimeErrorCode("local_runtime_ownership_failed")
        # Kill-on-close controls the complete child process tree; no model output is captured.
        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount", "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]
        class BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                        ("LimitFlags", ctypes.c_uint32), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", ctypes.c_uint32),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", ctypes.c_uint32), ("SchedulingClass", ctypes.c_uint32)]
        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BASIC_LIMIT_INFORMATION), ("IoInfo", IO_COUNTERS), ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = 0x2000
        kernel32.SetInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        if not kernel32.SetInformationJobObject(self.job_handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            self.close()
            raise RuntimeErrorCode("local_runtime_ownership_failed")
        env = os.environ.copy()
        env["LLAMA_API_KEY"] = self.api_key
        command = [str(self.runtime), "-m", str(self.model), "-ngl", "99", "-c", "4096", "-np", "1",
                   "--host", "127.0.0.1", "--port", str(self.port), "--no-context-shift", "--cors-origins", "localhost"]
        try:
            # Create suspended, assign the Job Object, and only then resume. This closes the
            # child-process escape window that Popen followed by assignment would leave.
            class STARTUPINFO(ctypes.Structure):
                _fields_ = [("cb", ctypes.c_uint32), ("lpReserved", ctypes.c_wchar_p), ("lpDesktop", ctypes.c_wchar_p),
                            ("lpTitle", ctypes.c_wchar_p), ("dwX", ctypes.c_uint32), ("dwY", ctypes.c_uint32),
                            ("dwXSize", ctypes.c_uint32), ("dwYSize", ctypes.c_uint32), ("dwXCountChars", ctypes.c_uint32),
                            ("dwYCountChars", ctypes.c_uint32), ("dwFillAttribute", ctypes.c_uint32), ("dwFlags", ctypes.c_uint32),
                            ("wShowWindow", ctypes.c_uint16), ("cbReserved2", ctypes.c_uint16), ("lpReserved2", ctypes.c_void_p),
                            ("hStdInput", ctypes.c_void_p), ("hStdOutput", ctypes.c_void_p), ("hStdError", ctypes.c_void_p)]
            class PROCESS_INFORMATION(ctypes.Structure):
                _fields_ = [("hProcess", ctypes.c_void_p), ("hThread", ctypes.c_void_p), ("dwProcessId", ctypes.c_uint32), ("dwThreadId", ctypes.c_uint32)]
            startup, process_info = STARTUPINFO(), PROCESS_INFORMATION()
            startup.cb = ctypes.sizeof(startup)
            env_block = "\0".join(f"{key}={value}" for key,value in sorted(env.items(), key=lambda item:item[0].upper())) + "\0\0"
            command_line = ctypes.create_unicode_buffer(subprocess.list2cmdline(command))
            kernel32.CreateProcessW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_void_p, ctypes.c_void_p,
                ctypes.c_int, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_void_p, ctypes.c_void_p]
            kernel32.CreateProcessW.restype = ctypes.c_int
            ok = kernel32.CreateProcessW(str(self.runtime), command_line, None, None, False, 0x00000004|0x00000400|0x08000000,
                                         ctypes.c_wchar_p(env_block), None, ctypes.byref(startup), ctypes.byref(process_info))
            if not ok:
                raise RuntimeErrorCode("local_runtime_start_failed")
            self.process_handle, self.thread_handle = process_info.hProcess, process_info.hThread
            kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            if not kernel32.AssignProcessToJobObject(self.job_handle, self.process_handle):
                kernel32.TerminateProcess(self.process_handle, 1)
                self.close()
                raise RuntimeErrorCode("local_runtime_ownership_failed")
            if kernel32.ResumeThread(self.thread_handle) == 0xFFFFFFFF:
                kernel32.TerminateJobObject(self.job_handle, 1)
                self.close()
                raise RuntimeErrorCode("local_runtime_start_failed")
            kernel32.CloseHandle(self.thread_handle); self.thread_handle=None
            deadline = time.monotonic() + 120
            client = httpx.Client(base_url=f"http://127.0.0.1:{self.port}", headers={"Authorization": f"Bearer {self.api_key}"}, timeout=2, trust_env=False, follow_redirects=False)
            try:
                while time.monotonic() < deadline:
                    if kernel32.WaitForSingleObject(self.process_handle, 0) == 0:
                        raise RuntimeErrorCode("local_runtime_start_failed")
                    try:
                        response = client.get("/health")
                        if response.status_code == 200:
                            return f"http://127.0.0.1:{self.port}"
                    except httpx.HTTPError:
                        pass
                    time.sleep(0.25)
            finally:
                client.close()
            raise RuntimeErrorCode("local_runtime_start_timeout")
        except BaseException:
            self.close()
            raise

    def close(self):
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True) if os.name == "nt" else None
        if self.job_handle:
            if not kernel32.CloseHandle(self.job_handle):
                raise RuntimeErrorCode("local_runtime_cleanup_failed")
            self.job_handle = None
        if self.process_handle:
            if kernel32.WaitForSingleObject(self.process_handle, 10000) != 0:
                raise RuntimeErrorCode("local_runtime_cleanup_failed")
            if not kernel32.CloseHandle(self.process_handle):
                raise RuntimeErrorCode("local_runtime_cleanup_failed")
            self.process_handle = None
        if self.thread_handle:
            kernel32.CloseHandle(self.thread_handle); self.thread_handle=None
        self.process = None
        self.api_key = None
