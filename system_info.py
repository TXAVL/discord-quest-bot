import os
import sys
import time
import shutil
import platform
import subprocess
from datetime import datetime
from typing import Dict, Any, Tuple

try:
    import psutil
except ImportError:
    psutil = None

try:
    import discord
except ImportError:
    discord = None


def make_bar(percent: float, length: int = 10) -> str:
    """Tạo thanh tiến trình trực quan bằng emoji / khối ký tự đẹp."""
    pct = max(0.0, min(100.0, float(percent)))
    filled = int(round((pct / 100.0) * length))
    empty = length - filled
    return "▰" * filled + "▱" * empty


def slider_bar(percent: float, length: int = 16) -> str:
    """Thanh tiến trình dạng slider mảnh: ━━━━●─────── (không dùng ô vuông)."""
    pct = max(0.0, min(100.0, float(percent)))
    pos = int(round((pct / 100.0) * (length - 1)))
    return "━" * pos + "●" + "─" * (length - 1 - pos)


def load_icon(percent: float) -> str:
    return "🟢" if percent < 60 else "🟡" if percent < 85 else "🔴"


def get_os_info() -> str:
    """Xác định chi tiết hệ điều hành (Ubuntu, Debian, Termux Android, Windows, macOS)."""
    # 1. Kiểm tra môi trường Termux trên Android
    is_termux = "TERMUX_VERSION" in os.environ or os.path.exists("/data/data/com.termux")
    if is_termux:
        termux_ver = os.environ.get("TERMUX_VERSION", "")
        model = ""
        android_ver = ""
        try:
            res = subprocess.run(["getprop", "ro.product.model"], capture_output=True, text=True, timeout=1)
            model = res.stdout.strip()
        except Exception:
            pass
        try:
            res = subprocess.run(["getprop", "ro.build.version.release"], capture_output=True, text=True, timeout=1)
            android_ver = res.stdout.strip()
        except Exception:
            pass
        
        desc = "📱 Android"
        if android_ver:
            desc += f" {android_ver}"
        if model:
            desc += f" ({model})"
        desc += f" • Termux{' v' + termux_ver if termux_ver else ''}"
        return desc

    # 2. Kiểm tra bản phân phối Linux (Ubuntu, Debian, CentOS, v.v.)
    if sys.platform.startswith("linux") and os.path.exists("/etc/os-release"):
        try:
            with open("/etc/os-release", "r", encoding="utf-8") as f:
                d = {}
                for line in f:
                    if "=" in line:
                        k, v = line.strip().split("=", 1)
                        d[k.strip()] = v.strip().strip('"')
                pretty_name = d.get("PRETTY_NAME") or d.get("NAME")
                if pretty_name:
                    return f"🐧 {pretty_name} ({platform.machine()})"
        except Exception:
            pass

    # 3. Windows
    if sys.platform == "win32":
        ver = platform.version()
        release = platform.release()
        return f"🪟 Windows {release} ({platform.machine()})"

    # 4. macOS
    if sys.platform == "darwin":
        mac_ver = platform.mac_ver()[0]
        return f"🍎 macOS {mac_ver} ({platform.machine()})"

    # 5. Mặc định
    return f"💻 {platform.system()} {platform.release()} ({platform.machine()})"


def get_cpu_info() -> Dict[str, Any]:
    """Lấy thông tin và phần trăm sử dụng CPU mà không nhất thiết phải có psutil."""
    cores = os.cpu_count() or 1
    cpu_model = ""

    # Thử lấy tên CPU từ /proc/cpuinfo (Linux & Termux Android)
    if os.path.exists("/proc/cpuinfo"):
        try:
            with open("/proc/cpuinfo", "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    if "model name" in line:
                        cpu_model = line.split(":", 1)[1].strip()
                        break
                    elif "Hardware" in line or "Processor" in line:
                        if not cpu_model:
                            cpu_model = line.split(":", 1)[1].strip()
        except Exception:
            pass

    # Windows CPU model
    if not cpu_model and sys.platform == "win32":
        try:
            cpu_model = platform.processor() or os.environ.get("PROCESSOR_IDENTIFIER", "")
            if "Family" in cpu_model and "Intel" in cpu_model:
                cpu_model = "Intel Core / Xeon Processor"
            elif "AMD" in cpu_model:
                cpu_model = "AMD Ryzen / EPYC Processor"
        except Exception:
            pass

    if not cpu_model:
        cpu_model = f"CPU đa nhân ({cores} Cores)"

    # Lấy % CPU
    cpu_percent = 0.0
    if psutil:
        try:
            cpu_percent = psutil.cpu_percent(interval=None)
        except Exception:
            pass

    # Fallback Linux/Termux loadavg nếu psutil = 0 hoặc None
    if cpu_percent == 0.0 and hasattr(os, "getloadavg"):
        try:
            load1, _, _ = os.getloadavg()
            cpu_percent = round(min(100.0, (load1 / cores) * 100.0), 1)
        except Exception:
            pass

    return {
        "model": cpu_model,
        "cores": cores,
        "percent": round(cpu_percent, 1)
    }


def get_ram_info() -> Dict[str, Any]:
    """Lấy thông số RAM hỗ trợ cả psutil, Linux /proc/meminfo, Termux và Windows ctypes."""
    if psutil:
        try:
            vm = psutil.virtual_memory()
            return {
                "total_gb": round(vm.total / (1024 ** 3), 2),
                "used_gb": round(vm.used / (1024 ** 3), 2),
                "percent": round(vm.percent, 1)
            }
        except Exception:
            pass

    # Fallback cho Linux & Termux Android (/proc/meminfo)
    if os.path.exists("/proc/meminfo"):
        try:
            info = {}
            with open("/proc/meminfo", "r", encoding="utf-8") as f:
                for line in f:
                    parts = line.split(":")
                    if len(parts) == 2:
                        k = parts[0].strip()
                        v = parts[1].strip().split()[0]
                        info[k] = int(v)
            total_kb = info.get("MemTotal", 0)
            avail_kb = info.get("MemAvailable", info.get("MemFree", 0) + info.get("Buffers", 0) + info.get("Cached", 0))
            used_kb = max(0, total_kb - avail_kb)
            if total_kb > 0:
                percent = round((used_kb / total_kb) * 100, 1)
                total_gb = round(total_kb / (1024 * 1024), 2)
                used_gb = round(used_kb / (1024 * 1024), 2)
                return {
                    "total_gb": total_gb,
                    "used_gb": used_gb,
                    "percent": percent
                }
        except Exception:
            pass

    # Fallback cho Windows dùng ctypes
    if sys.platform == "win32":
        try:
            import ctypes
            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]
            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
            total_gb = round(stat.ullTotalPhys / (1024 ** 3), 2)
            used_gb = round((stat.ullTotalPhys - stat.ullAvailPhys) / (1024 ** 3), 2)
            percent = float(stat.dwMemoryLoad)
            return {
                "total_gb": total_gb,
                "used_gb": used_gb,
                "percent": percent
            }
        except Exception:
            pass

    return {"total_gb": 0.0, "used_gb": 0.0, "percent": 0.0}


def get_disk_info() -> Dict[str, Any]:
    """Lấy thông số ổ đĩa bằng built-in shutil."""
    try:
        path = "." if os.name == "nt" else "/"
        usage = shutil.disk_usage(path)
        total_gb = round(usage.total / (1024 ** 3), 2)
        used_gb = round(usage.used / (1024 ** 3), 2)
        percent = round((usage.used / usage.total) * 100, 1) if usage.total > 0 else 0.0
        return {
            "total_gb": total_gb,
            "used_gb": used_gb,
            "percent": percent
        }
    except Exception:
        return {"total_gb": 0.0, "used_gb": 0.0, "percent": 0.0}


def get_process_memory() -> float:
    """Lấy lượng RAM riêng mà tiến trình Bot đang tiêu thụ (MB)."""
    if psutil:
        try:
            return round(psutil.Process().memory_info().rss / (1024 * 1024), 1)
        except Exception:
            pass

    # Linux / Termux /proc/self/status
    if os.path.exists("/proc/self/status"):
        try:
            with open("/proc/self/status", "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        kb = int(line.split()[1])
                        return round(kb / 1024, 1)
        except Exception:
            pass

    # Windows GetProcessMemoryInfo
    if sys.platform == "win32":
        try:
            import ctypes
            class PMC(ctypes.Structure):
                _fields_ = [
                    ("cb", ctypes.c_ulong),
                    ("PageFaultCount", ctypes.c_ulong),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]
            pmc = PMC()
            pmc.cb = ctypes.sizeof(PMC)
            kernel32 = ctypes.windll.kernel32
            psapi = ctypes.windll.psapi
            psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(PMC), ctypes.c_ulong]
            if psapi.GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(pmc), ctypes.sizeof(PMC)):
                return round(pmc.WorkingSetSize / (1024 * 1024), 1)
        except Exception:
            pass

    return 0.0


def get_active_sessions_count() -> int:
    """Đếm chính xác số phiên làm quest đang hoạt động trực tiếp trong bộ nhớ."""
    import config
    dead_uids = []
    for uid, info in list(config.ACTIVE_USERS.items()):
        task = info.get("task")
        if task and task.done():
            dead_uids.append(uid)
    for uid in dead_uids:
        config.ACTIVE_USERS.pop(uid, None)
    return len(config.ACTIVE_USERS)


def get_realtime_quest_stats() -> Tuple[int, int]:
    """Lấy số lượng quest thành công & thất bại thời gian thực, đọc trực tiếp từ dữ liệu lưu trữ."""
    import config
    from limits_manager import load_limits
    limits_data = load_limits()

    file_completed = limits_data.get("total_completed", 0)
    file_failed = limits_data.get("total_failed", 0)

    # Đếm cộng dồn từ từng tài khoản để tuyệt đối chính xác
    user_sum = 0
    for k, v in limits_data.items():
        if isinstance(v, dict) and "total_completed" in v:
            user_sum += v.get("total_completed", 0)

    total_completed = max(config.TOTAL_COMPLETED, file_completed, user_sum)
    total_failed = max(config.TOTAL_FAILED, file_failed)

    config.TOTAL_COMPLETED = total_completed
    config.TOTAL_FAILED = total_failed
    return total_completed, total_failed
