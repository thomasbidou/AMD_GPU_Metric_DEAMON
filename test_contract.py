"""Vérifie que le shape JSON du daemon AMD satisfait le contrat de
l'intégration Home Assistant `nvidia_gpu` (clés attendues par
coordinator._EXPECTED_KEYS + gpus[] + cpu)."""
import importlib.util

spec = importlib.util.spec_from_file_location("amd_gpu_stats", "amd_gpu_stats.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

data = m.collect()
assert data is not None, "collect() retourne None !"

EXPECTED_TOP = ("gpu_utilization_pct", "memory_used_pct", "memory_used_gib",
                "memory_total_gib", "power_draw_w", "power_limit_w",
                "power_usage_pct", "temperature_c", "fan_speed_pct")
GPU_KEYS = ("name", "uuid", "driver_version", "gpu_utilization_pct",
            "memory_used_pct", "memory_used_gib", "memory_total_gib",
            "memory_controller_util_pct", "power_draw_w", "power_limit_w",
            "power_usage_pct", "temperature_c", "fan_speed_pct")
CPU_KEYS = ("name", "usage_pct", "temperature_c", "ram_used_gib",
            "ram_total_gib", "ram_used_pct")

ok = True
for k in EXPECTED_TOP:
    if k not in data:
        print("MANQUANT top-level:", k); ok = False
g = data["gpus"][0]
for k in GPU_KEYS:
    if k not in g:
        print("MANQUANT gpus[0]:", k); ok = False
for k in CPU_KEYS:
    if k not in data["cpu"]:
        print("MANQUANT cpu:", k); ok = False
if "box" not in data:
    print("MANQUANT box"); ok = False
if data["name"] != g["name"]:
    print("v1-compat: top-level name != gpus[0].name"); ok = False

print("CONTRAT HA:", "OK — toutes les clés présentes (v1 + v2 + cpu)" if ok else "ECHEC")
print("GPU      :", g["name"])
print("Util     :", g["gpu_utilization_pct"], "%")
print("Temp     :", g["temperature_c"], "°C")
print("VRAM     :", g["memory_used_gib"], "/", g["memory_total_gib"], "GiB (", g["memory_used_pct"], "% )")
print("Power    :", g["power_draw_w"], "W (limit:", str(g["power_limit_w"]) + ")")
print("Fan      :", g["fan_speed_pct"])
print("CPU      :", data["cpu"]["name"], "| usage:", data["cpu"]["usage_pct"], "% | temp:", data["cpu"]["temperature_c"], "°C")
print("RAM      :", data["cpu"]["ram_used_gib"], "/", data["cpu"]["ram_total_gib"], "GiB")
print("Box      :", data["box"])
