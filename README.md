# AMD GPU Stats — daemon métriques GPU/APU AMD (Radeon 8060S)

Daemon Python **stdlib uniquement** (aucune dépendance tierce) qui lit les
métriques d'un GPU/APU AMD sur Linux via `rocm-smi` + `amd-smi` + le hwmon
`amdgpu`/`k10temp` + `/proc`, et sert un **snapshot JSON** sur HTTP pour
qu'une instance Home Assistant (ou n'importe quel poller) puisse le
consommer sans driver AMD.

Il reproduit **exactement** le shape JSON du daemon NVIDIA existant
(`NVIDIA_GPU_Metric_DEAMON/server.py`), donc l'intégration HA `nvidia_gpu`
fonctionne **sans aucun changement** (mêmes clés, mêmes types).

## Endpoints

| Route | Rôle |
|-------|------|
| `GET /` | Snapshot JSON (dernier poll) — 503 tant que le premier poll n'a pas réussi |
| `GET /health` | `{"status": "ok"}` |

Headers `Access-Control-Allow-Origin: *` sur toutes les réponses (CORS OK).

## Variables d'environnement

| Variable | Défaut | Description |
|----------|--------|-------------|
| `AMD_GPU_STATS_PORT` | `8791` | Port HTTP |
| `AMD_GPU_STATS_POLL` | `3` | Intervalle de poll (secondes) |

## Source de chaque métrique (Strix Halo / APU)

| Champ | Source primaire | Source de secours |
|-------|-----------------|-------------------|
| `name`, `uuid`, `driver_version` | `rocm-smi --showid --json` / `--showdriverversion` | — |
| `gpu_utilization_pct` | `rocm-smi` `GPU use (%)` | — |
| `temperature_c` | `rocm-smi` `Temperature (Sensor edge) (C)` | hwmon `amdgpu` `temp1_input` |
| `power_draw_w` | `rocm-smi` `Current Socket Graphics Package Power (W)` | hwmon `amdgpu` `power1_input` (µW) |
| `memory_used_gib` / `memory_total_gib` / `memory_used_pct` | `amd-smi` overview `used/total MB` | `rocm-smi` `VRAM%` (pour le % seul) |
| `memory_controller_util_pct` | `rocm-smi` `Memory Activity` | `null` si N/A |
| `fan_speed_pct` | — | **`null`** (iGPU passive, pas de ventilo) |
| `power_limit_w` / `power_usage_pct` | — | **`null`** (pas de TDP fixe exposé sur l'APU) |
| CPU `usage_pct` | `/proc/stat` (échantillonnage 0,5 s) | — |
| CPU `temperature_c` | hwmon `k10temp` `Tctl` | — |
| RAM `*_gib` / `ram_used_pct` | `/proc/meminfo` | — |
| `box` | `socket.gethostname()` | — |

## Limites APU / iGPU (à connaître)

- **Ventilateur : `null`.** La Radeon 8060S est une iGPU passive
  (dissipation par la plaque/le chassis) — ni `rocm-smi` ni `amd-smi` n'expose
  de vitesse de ventilateur (les deux renvoient `N/A`). Jamais de `0` inventé.
- **Mémoire unifiée.** Le GPU et le CPU partagent les 124 Go de RAM.
  `amd-smi` expose la **tranche de mémoire adressable par le GPU**
  (`used/total MB`) ; c'est ce que nous renvoyons dans
  `memory_used_gib`/`memory_total_gib`/`memory_used_pct`. Ce n'est **pas** la
  RAM système (celle-là est dans `cpu.ram_*`).
- **Puissance : `power_limit_w` et `power_usage_pct` = `null`.** L'APU n'expose
  pas de plafond TDP fixe ; on ne peut donc pas calculer un % de puissance.
  `power_draw_w` est fourni (consommation socket/PPT réelle).
- **`memory_controller_util_pct` = `null`** quand `Memory Activity` renvoie
  `N/A` (cas observé ici) — c'est la bande passante du contrôleur mémoire,
  distincte du % d'occupation.

## Fichiers

| Fichier | Rôle |
|---------|------|
| `amd_gpu_stats.py` | Le daemon (stdlib uniquement) |
| `amd-gpu-stats.service` | Unité systemd |
| `test_contract.py` | Test du contrat JSON (vérifie toutes les clés attendues par l'intégration HA) |
| `README.md` | Ce document |

## Installation (systemd)

```bash
# 0) ajuster WorkingDirectory / ExecStart / User= dans l'unité si besoin
#    (défaut : /home/thomas/workspace/AMD-GPU-Mon/amd_gpu_stats)
# 1) copier l'unité
sudo cp amd-gpu-stats.service /etc/systemd/system/
# 2) recharger + activer
sudo systemctl daemon-reload
sudo systemctl enable --now amd-gpu-stats
# 3) vérifier
systemctl status amd-gpu-stats
curl -s http://127.0.0.1:8791/health
curl -s http://127.0.0.1:8791/ | python3 -m json.tool
```

### Lancer sans systemd (test manuel)

```bash
AMD_GPU_STATS_PORT=8791 python3 amd_gpu_stats.py
# autre terminal :
curl -s http://127.0.0.1:8791/
curl -s http://127.0.0.1:8791/health
```

## Compatibilité Home Assistant

Le daemon émet les **mêmes clés** attendues par l'intégration `nvidia_gpu`
(cf. `nvidia_gpu/coordinator.py` `_EXPECTED_KEYS` et `sensor.py`
`GPU_SENSORS` / `SYSTEM_SENSORS`) :

- GPU : `gpu_utilization_pct`, `memory_used_pct`, `memory_used_gib`,
  `memory_total_gib`, `power_draw_w`, `power_limit_w`, `power_usage_pct`,
  `temperature_c`, `fan_speed_pct` (+ `name`, `uuid`, `driver_version`,
  `memory_controller_util_pct`)
- Système : `cpu.name`, `cpu.usage_pct`, `cpu.temperature_c`,
  `cpu.ram_used_gib`, `cpu.ram_total_gib`, `cpu.ram_used_pct`
- Métadonnées : `box`, `poll_interval_s`, `collected_at`, `gpus[]`

L'intégration lit la liste `gpus[]` (shape v2) avec repli sur les clés top-level
(shape v1, également fournies). **Zéro modification HA requise** — il suffit de
pointer l'intégration sur `http://<machine>:8791/`.

> Note : le champ `box` (hostname) est utilisé par l'intégration pour
> disambiguer plusieurs boîtiers ; il est correctement rempli ici.

## Robustesse

- Si `rocm-smi`/`amd-smi` échouent ou renvoie une sortie illisible, le daemon
  **logue** l'erreur et renvoie **503** (jamais 500) sur `GET /` ; le poll
  suivant retente automatiquement.
- Chaque métrique réellement indisponible renvoie **`null`** (jamais `0`
  inventé).
- `GET /health` renvoie toujours `200 {"status":"ok"}` tant que le processus
  tourne (indépendant du succès du dernier poll).

## ⚠️ Disclaimer

Ce programme a été développé **entièrement avec l'aide d'une IA** (agent
Hermes / modèles de langage). Le code est fourni **tel quel, sans garantie** —
vérifiez-le avant de l'exploiter en production.

## Licence

MIT.
