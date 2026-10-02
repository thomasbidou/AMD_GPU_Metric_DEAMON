#!/usr/bin/env python3
"""amd_gpu stats daemon (Strix Halo / APU Radeon 8060S, unified memory).

Polls `rocm-smi` (+ `amd-smi` for VRAM GiB) and the `amdgpu`/`k10temp`
hwmon nodes, plus /proc system metrics (CPU %, CPU temp, RAM), and serves
a small JSON document over HTTP so a remote Home Assistant instance can
poll it without needing the AMD stack.

Stdlib only. No third-party deps.

Endpoints:
  GET /            -> JSON snapshot (latest poll)
  GET /health      -> {"status": "ok"}

Response shape (v2 with a "gpus" list; v1 first-GPU fields also at top
level for backward compat with the existing `nvidia_gpu` integration):
  {
    "gpus": [ {
      "name", "uuid", "driver_version",
      "gpu_utilization_pct", "memory_used_pct",
      "memory_used_gib", "memory_total_gib",
      "memory_controller_util_pct",
      "power_draw_w", "power_limit_w", "power_usage_pct",
      "temperature_c", "fan_speed_pct"
    } ],
    # v1 compat: first GPU's fields promoted to top level (incl. "name")
    "box": "<hostname>",
    "cpu": { "name", "usage_pct", "temperature_c",
             "ram_used_gib", "ram_total_gib", "ram_used_pct" },
    "poll_interval_s": 3.0,
    "collected_at": <unix ts>
  }

APU / iGPU notes (values that are genuinely unavailable are null, never 0):
  * fan_speed_pct        -> null  (passive iGPU, no fan: rocm-smi/amd-smi say N/A)
  * power_limit_w        -> null  (no fixed TDP cap exposed on the APU)
  * power_usage_pct      -> null  (needs a power limit to compute a ratio)
  * memory_controller_util_pct -> null when "Memory Activity" is N/A
  * memory_used_gib / memory_total_gib -> the *unified-memory* slice the
    GPU can address, read from `amd-smi` (used/total MB); null if amd-smi
    is unavailable.
"""
from __future__ import annotations

import glob
import json
import logging
import os
import re
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

HOST = "0.0.0.0"
PORT = int(os.environ.get("AMD_GPU_STATS_PORT", "8791"))
POLL_INTERVAL_S = float(os.environ.get("AMD_GPU_STATS_POLL", "3"))

# CPU usage: sample /proc/stat over this window (seconds).
CPU_SAMPLE_S = 0.5
# CPU temp: prefer k10temp (Tctl), the standard AMD SoC control temp.
CPU_TEMP_CHIP = "k10temp"

# Timeout for each rocm-smi / amd-smi subprocess (seconds).
SUBPROC_TIMEOUT_S = 10

# rocm-smi flags we combine into a single --json call (one subprocess, one
# parse -> most reliable + cheapest way to read the AMD metrics).
ROCM_FLAGS = [
    "--showuse",      # GPU use (%)
    "--showmemuse",   # GPU Memory Allocated (VRAM%), Memory Activity
    "--showpower",    # Current Socket Graphics Package Power (W)
    "--showtemp",     # Temperature (Sensor edge) (C)
    "--showid",       # Device Name, Device ID, GUID
    "--json",
]

log = logging.getLogger("amd_gpu_stats")


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------
def _to_float(s: Optional[str]) -> Optional[float]:
    """Parse a numeric token out of an AMD/rocm-smi field.

    Handles the N/A variants and returns None when the value is genuinely
    unavailable (never invents 0).
    """
    if s is None:
        return None
    s = str(s).strip()
    if not s or s.upper() in (
        "N/A", "NA", "[N/A]", "[Not Supported]", "NOT SUPPORTED",
        "N/A [Not Supported]", "NOT AVAILABLE", "UNKNOWN",
    ):
        return None
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    if not m:
        return None
    return float(m.group(0))


def _to_int(s: Optional[str]) -> Optional[int]:
    f = _to_float(s)
    return None if f is None else int(f)


def _read_int(path: str) -> Optional[int]:
    try:
        with open(path) as fh:
            return int(fh.read().strip())
    except (OSError, ValueError):
        return None


def _read_str(path: str) -> Optional[str]:
    try:
        with open(path) as fh:
            return fh.read().strip() or None
    except OSError:
        return None


def _run(cmd: list[str], timeout: int = SUBPROC_TIMEOUT_S) -> Optional[str]:
    """Run a subprocess and return stdout, or None on any failure."""
    try:
        out = subprocess.check_output(cmd, text=True, timeout=timeout)
        return out
    except subprocess.SubprocessError as exc:
        log.warning("subprocess failed (%s): %s", " ".join(cmd[:3]), exc)
        return None
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("subprocess error (%s): %s", " ".join(cmd[:3]), exc)
        return None


def _gib(v: Optional[float]) -> Optional[float]:
    return None if v is None else round(v, 3)


# --------------------------------------------------------------------------
# AMD metric sources
# --------------------------------------------------------------------------
def _rocm_json() -> Optional[dict]:
    """Parse `rocm-smi <flags> --json` -> { "cardN": {metric: value}, ... }.

    Returns None on any failure so callers can fall back.
    """
    out = _run(["rocm-smi"] + ROCM_FLAGS)
    if out is None:
        return None
    # The JSON is the last line; strip any non-JSON preamble just in case.
    txt = out.strip()
    start = txt.find("{")
    if start == -1:
        log.warning("rocm-smi --json returned no JSON object")
        return None
    try:
        data = json.loads(txt[start:])
    except json.JSONDecodeError as exc:
        log.warning("rocm-smi --json parse error: %s", exc)
        return None
    if not isinstance(data, dict) or not data:
        log.warning("rocm-smi --json returned an empty/non-dict payload")
        return None
    return data


def _rocm_driver_version() -> Optional[str]:
    """`rocm-smi --showdriverversion` -> 'Driver version: <X>'."""
    out = _run(["rocm-smi", "--showdriverversion"])
    if out is None:
        return None
    m = re.search(r"Driver version:\s*(\S+)", out)
    if m:
        return m.group(1)
    # Fallback: the kernel release is what AMD reports as the driver here.
    try:
        with open("/proc/sys/kernel/osrelease") as fh:
            return fh.read().strip()
    except OSError:
        return None


def _amdgpu_hwmon() -> Optional[str]:
    """Return the hwmon dir backing the amdgpu driver (card0), or None."""
    try:
        links = sorted(glob.glob("/sys/class/drm/card*/device/hwmon/hwmon*"))
    except OSError:
        return None
    for link in links:
        try:
            target = os.path.realpath(link)
            if _read_str(os.path.join(target, "name")) == "amdgpu":
                return target
        except OSError:
            continue
    return None


def _hwmon_gpu_temp_c() -> Optional[int]:
    base = _amdgpu_hwmon()
    if base is None:
        return None
    v = _read_int(os.path.join(base, "temp1_input"))  # millidegree C
    return None if v is None else v // 1000


def _hwmon_gpu_power_w() -> Optional[float]:
    base = _amdgpu_hwmon()
    if base is None:
        return None
    v = _read_int(os.path.join(base, "power1_input"))  # micro-watt
    return None if v is None else round(v / 1_000_000.0, 3)


def _amdsmi_mem_mb() -> list[tuple[Optional[float], Optional[float]]]:
    """Parse `amd-smi` overview -> list of (used_mb, total_mb) per GPU.

    The overview has one "NNNNN/NNNNN MB" pair per GPU (unified-memory
    slice the GPU can address). This is the *only* source that exposes the
    GiB figures on this APU, so it is the authoritative memory source.
    """
    out = _run(["amd-smi"])
    if out is None:
        return []
    pairs = re.findall(r"(\d+)/(\d+)\s*MB", out)
    result: list[tuple[Optional[float], Optional[float]]] = []
    for used, total in pairs:
        result.append((float(used), float(total)))
    return result


# --------------------------------------------------------------------------
# GPU assembly
# --------------------------------------------------------------------------
def _gpu_from_sources(
    idx: int,
    rocm: Optional[dict],
    driver_version: Optional[str],
    mem_pairs: list[tuple[Optional[float], Optional[float]]],
) -> dict:
    """Build one GPU dict (all target keys present; null where unknown)."""
    # Primary: rocm-smi --json for this card (key "cardN").
    card = {}
    if isinstance(rocm, dict):
        card = rocm.get(f"card{idx}") or {}

    def cval(key: str) -> Optional[str]:
        v = card.get(key)
        return v if isinstance(v, str) else None

    # --- identity ---
    name = cval("Device Name") or f"AMD GPU {idx}"
    uuid = cval("GUID") or cval("Device ID") or f"amd-gpu-{idx}"

    # --- utilization ---
    gpu_util = _to_float(cval("GPU use (%)"))

    # --- temperature (primary rocm edge; fallback hwmon amdgpu) ---
    temp_c = _to_int(cval("Temperature (Sensor edge) (C)"))
    if temp_c is None:
        temp_c = _hwmon_gpu_temp_c()

    # --- power draw (primary rocm socket PPT; fallback hwmon) ---
    power_draw_w = _to_float(cval("Current Socket Graphics Package Power (W)"))
    if power_draw_w is None:
        power_draw_w = _hwmon_gpu_power_w()

    # --- memory controller / bandwidth utilization ---
    mem_activity = cval("Memory Activity")  # N/A on this APU -> None
    mem_controller_util = _to_float(mem_activity)

    # --- VRAM (unified-memory slice) : authoritative from amd-smi ---
    mem_used_gib = mem_total_gib = None
    mem_used_pct = None
    if idx < len(mem_pairs) and mem_pairs[idx] is not None:
        used_mb, total_mb = mem_pairs[idx]
        mem_used_gib = round(used_mb / 1024.0, 3)
        mem_total_gib = round(total_mb / 1024.0, 3)
        if total_mb:
            mem_used_pct = round(100.0 * used_mb / total_mb, 1)
    else:
        # rocm-smi only exposes a VRAM % (no GiB) — use it for pct only.
        vram_pct = cval("GPU Memory Allocated (VRAM%)")
        if vram_pct:
            mem_used_pct = _to_float(vram_pct)

    # --- APU / iGPU invariants (genuinely unavailable -> null) ---
    # Passive iGPU: no fan. No fixed TDP cap exposed on the APU.
    fan_speed_pct = None
    power_limit_w = None
    power_usage_pct = (
        round(100.0 * power_draw_w / power_limit_w, 2)
        if (power_draw_w is not None and power_limit_w)
        else None
    )

    return {
        "name": name,
        "uuid": uuid,
        "driver_version": driver_version,
        "gpu_utilization_pct": gpu_util,
        "memory_used_pct": mem_used_pct,
        "memory_used_gib": mem_used_gib,
        "memory_total_gib": mem_total_gib,
        "memory_controller_util_pct": mem_controller_util,
        "power_draw_w": power_draw_w,
        "power_limit_w": power_limit_w,
        "power_usage_pct": power_usage_pct,
        "temperature_c": temp_c,
        "fan_speed_pct": fan_speed_pct,
    }


def _gpu_has_any_value(g: dict) -> bool:
    """True if this GPU dict carries at least one real measurement."""
    probe_keys = (
        "gpu_utilization_pct", "memory_used_gib", "memory_total_gib",
        "memory_used_pct", "power_draw_w", "temperature_c",
    )
    return any(g.get(k) is not None for k in probe_keys) or bool(g.get("name"))


def collect() -> Optional[dict]:
    """Read AMD sources + system metrics and return a snapshot dict.

    Returns None (-> 503) only if we can't produce a single GPU at all.
    """
    rocm = _rocm_json()
    driver_version = _rocm_driver_version()
    mem_pairs = _amdsmi_mem_mb()

    # How many GPUs? rocm-smi JSON keys (card0..N) define the count; if
    # rocm-smi failed we still emit a single GPU so the card stays visible.
    n_cards = len(rocm) if isinstance(rocm, dict) and rocm else 1

    gpus: list[dict] = []
    for idx in range(n_cards):
        g = _gpu_from_sources(idx, rocm, driver_version, mem_pairs)
        if _gpu_has_any_value(g):
            gpus.append(g)

    if not gpus:
        log.warning("no usable GPU data from any AMD source")
        return None

    first = gpus[0]
    return {
        # v2: explicit list (may contain multiple GPUs)
        "gpus": gpus,
        # v1 compat: first GPU promoted to top level
        **first,
        # system
        "box": socket.gethostname(),
        "cpu": collect_system(),
        "poll_interval_s": POLL_INTERVAL_S,
        "collected_at": time.time(),
    }


# --------------------------------------------------------------------------
# system metrics (CPU %, CPU temp, RAM) — same logic as the NVIDIA daemon
# --------------------------------------------------------------------------
def _cpu_usage_pct() -> Optional[float]:
    """Sample /proc/stat twice and compute busy% over the window."""
    def sample():
        try:
            with open("/proc/stat") as fh:
                line = fh.readline().split()
        except OSError:
            return None
        nums = list(map(int, line[1:]))
        idle = nums[3] + (nums[4] if len(nums) > 4 else 0)
        total = sum(nums)
        return idle, total

    s1 = sample()
    if s1 is None:
        return None
    time.sleep(CPU_SAMPLE_S)
    s2 = sample()
    if s2 is None:
        return None
    (idle1, total1), (idle2, total2) = s1, s2
    d_total = total2 - total1
    if d_total <= 0:
        return None
    d_busy = d_total - (idle2 - idle1)
    return round(100.0 * d_busy / d_total, 1)


def _cpu_temperature_c() -> Optional[int]:
    """Find the CPU core temperature from /sys/class/hwmon (k10temp Tctl)."""
    base = "/sys/class/hwmon"
    try:
        chips = sorted(os.listdir(base))
    except OSError:
        return None

    def temp_of(chip: str) -> Optional[int]:
        cdir = os.path.join(base, chip)
        for f in sorted(glob.glob(os.path.join(cdir, "temp*_input"))):
            label = _read_str(f.replace("_input", "_label")) or ""
            if "Tctl" in label:
                v = _read_int(f)
                if v is not None:
                    return v // 1000
        v = _read_int(os.path.join(cdir, "temp1_input"))
        return None if v is None else v // 1000

    for chip in chips:
        if chip == CPU_TEMP_CHIP:
            t = temp_of(chip)
            if t is not None:
                return t
    for chip in chips:
        name = (_read_str(os.path.join(base, chip, "name")) or "").lower()
        if name in ("k10temp", "cpu_thermal", "coretemp", "zenpower", "cpu"):
            t = temp_of(chip)
            if t is not None:
                return t
    return None


def _cpu_name() -> Optional[str]:
    try:
        with open("/proc/cpuinfo") as fh:
            for line in fh:
                if line.lower().startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return None


def collect_system() -> dict:
    """Collect CPU %, CPU temp, and RAM metrics."""
    def gib_kb(v: Optional[int]) -> Optional[float]:
        return None if v is None else round(v / 1024.0 / 1024.0, 3)

    mem: dict[str, int] = {}
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                key, _, rest = line.partition(":")
                val = rest.strip().split()
                if val:
                    mem[key] = int(val[0])
    except OSError:
        pass

    ram_total_kb = mem.get("MemTotal")
    ram_available_kb = mem.get("MemAvailable")
    ram_used_kb = (
        (ram_total_kb - ram_available_kb)
        if (ram_total_kb and ram_available_kb is not None)
        else None
    )
    ram_used_gib = gib_kb(ram_used_kb)
    ram_total_gib = gib_kb(ram_total_kb)
    ram_used_pct = (
        round(100.0 * ram_used_kb / ram_total_kb, 1)
        if (ram_used_kb is not None and ram_total_kb)
        else None
    )

    return {
        "name": _cpu_name() or "CPU",
        "usage_pct": _cpu_usage_pct(),
        "temperature_c": _cpu_temperature_c(),
        "ram_used_gib": ram_used_gib,
        "ram_total_gib": ram_total_gib,
        "ram_used_pct": ram_used_pct,
    }


# --------------------------------------------------------------------------
# HTTP server
# --------------------------------------------------------------------------
class State:
    """Thread-safe holder for the latest snapshot."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.data: Optional[dict] = None
        self.error: Optional[str] = None
        self.last_ok: Optional[float] = None

    def set(self, data: Optional[dict], err: Optional[str] = None) -> None:
        with self._lock:
            if data is not None:
                self.data = data
                self.error = None
                self.last_ok = time.time()
            else:
                self.error = err or "collection failed"

    def get(self) -> dict:
        with self._lock:
            return {"data": self.data, "error": self.error, "last_ok": self.last_ok}


STATE = State()


def poll_loop() -> None:
    while True:
        started = time.time()
        data = collect()
        STATE.set(data, err=None if data is not None else "AMD metric collection failed")
        if data is None:
            log.debug("poll failed")
        elapsed = time.time() - started
        time.sleep(max(0.2, POLL_INTERVAL_S - elapsed))


class Handler(BaseHTTPRequestHandler):
    server_version = "amd-gpu-stats/1.0"

    def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/")
        if path in ("", "/"):
            snap = STATE.get()
            if not snap["data"]:
                self._send(
                    503,
                    json.dumps({"error": snap["error"] or "not ready"}).encode(),
                )
                return
            self._send(200, json.dumps(snap["data"], indent=2).encode())
        elif path == "/health":
            self._send(200, json.dumps({"status": "ok"}).encode())
        else:
            self._send(404, b'{"error":"not found"}')

    def log_message(self, fmt: str, *args) -> None:  # quiet access log
        log.debug("http: " + fmt, *args)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Prime the first snapshot before we start serving so the first request
    # doesn't 503.
    first = collect()
    STATE.set(first, err=None if first is not None else "initial collection failed")
    threading.Thread(target=poll_loop, name="poll", daemon=True).start()

    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    log.info("amd_gpu_stats listening on http://%s:%d/", HOST, PORT)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
