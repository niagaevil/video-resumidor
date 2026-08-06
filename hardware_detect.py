"""
Deteccao de hardware para recomendacao automatica de modelos.

Detecta RAM total, GPU e VRAM em Windows, Linux e WSL.
Sem dependencias externas — usa apenas modulos da stdlib.
"""

import ctypes
import os
import platform
import re
import subprocess
import sys


def _run(cmd, timeout=5):
    """Executa comando e retorna stdout, ou '' em caso de erro."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip() if r.returncode == 0 else ''
    except Exception:
        return ''


# ── RAM ──────────────────────────────────────────────────────────────────────

def _ram_windows():
    """RAM total no Windows via kernel32."""
    try:
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
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]
        m = MEMORYSTATUSEX()
        m.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
        return int(m.ullTotalPhys)
    except Exception:
        return 0


def _ram_linux():
    """RAM total no Linux via /proc/meminfo."""
    try:
        with open('/proc/meminfo', encoding='utf-8') as f:
            for line in f:
                if line.startswith('MemTotal:'):
                    kb = int(re.findall(r'\d+', line)[0])
                    return kb * 1024
    except Exception:
        pass
    return 0


def get_ram_bytes():
    """RAM total em bytes. 0 se nao detectado."""
    if sys.platform == 'win32':
        return _ram_windows()
    return _ram_linux()


def get_ram_gb():
    """RAM total em GB (arredondado)."""
    b = get_ram_bytes()
    return round(b / (1024 ** 3)) if b else 0


# ── GPU / VRAM ────────────────────────────────────────────────────────────────

def _parse_nvidia_smi():
    """Extrai nome e VRAM do nvidia-smi."""
    out = _run([
        'nvidia-smi',
        '--query-gpu=name,memory.total',
        '--format=csv,noheader',
    ])
    if not out:
        return []
    gpus = []
    for line in out.splitlines():
        parts = [p.strip() for p in line.split(',')]
        if len(parts) >= 2:
            name = parts[0]
            vram_mb = int(re.findall(r'\d+', parts[1])[0])
            gpus.append({'name': name, 'vram_mb': vram_mb})
    return gpus


def _gpu_windows():
    """GPUs no Windows via wmic."""
    out = _run([
        'wmic', 'path', 'win32_VideoController',
        'get', 'name,AdapterRAM', '/format:csv'
    ])
    if not out:
        return []
    gpus = []
    for line in out.splitlines()[1:]:  # skip header
        parts = [p.strip() for p in line.split(',')]
        if len(parts) >= 3:
            name = parts[2]
            try:
                vram_bytes = int(parts[1])
            except ValueError:
                vram_bytes = 0
            if name and vram_bytes > 0:
                gpus.append({'name': name, 'vram_mb': vram_bytes // (1024 * 1024)})
    return gpus


def _gpu_linux():
    """GPUs no Linux via lspci."""
    out = _run(['lspci'])
    if not out:
        return []
    gpus = []
    for line in out.splitlines():
        if 'vga' in line.lower() or '3d' in line.lower() or 'display' in line.lower():
            # Extrai nome apos o ": "
            match = re.search(r': (.+)$', line)
            name = match.group(1) if match else line
            gpus.append({'name': name.strip(), 'vram_mb': 0})
    return gpus


def get_gpus():
    """
    Retorna lista de GPUs detectadas.
    Cada entrada: {'name': str, 'vram_mb': int} (0 se VRAM desconhecida).
    """
    # Tenta nvidia-smi primeiro (mais preciso)
    gpus = _parse_nvidia_smi()
    if gpus:
        return gpus

    # Fallback especifico por SO
    if sys.platform == 'win32':
        gpus = _gpu_windows()
    else:
        gpus = _gpu_linux()

    return gpus


def get_total_vram_mb():
    """VRAM total somada de todas as GPUs detectadas."""
    return sum(g['vram_mb'] for g in get_gpus())


# ── Tier ──────────────────────────────────────────────────────────────────────

def detect_tier():
    """
    Detecta o tier de hardware e retorna um dict:
    {
        'tier': 'low' | 'mid' | 'high',
        'ram_gb': int,
        'gpus': [{'name': str, 'vram_mb': int}, ...],
        'total_vram_gb': float,
    }
    """
    ram = get_ram_gb()
    gpus = get_gpus()
    vram_mb = get_total_vram_mb()
    vram_gb = round(vram_mb / 1024, 1)

    # Heuristica de tier
    if ram <= 10 or (vram_mb < 3000 and not gpus):
        tier = 'low'
    elif ram >= 28 and vram_mb >= 10000:
        tier = 'high'
    elif ram >= 14 and vram_mb >= 4000:
        tier = 'mid'
    elif ram >= 16:
        tier = 'mid'   # RAM suficiente, mesmo sem GPU detectada
    else:
        tier = 'low'

    return {
        'tier': tier,
        'ram_gb': ram,
        'gpus': gpus,
        'total_vram_gb': vram_gb if vram_mb > 0 else 0,
    }
