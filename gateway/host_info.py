"""
Read-only знімок хоста VM для /admin/diag (секція `host`): GCE metadata, ресурси, сервіси, таймери, cron, docker,
топ процесів за памʼяттю (лише імена), порти, що слухають. Усі виводи проходять redact(); жодних аргументів процесів.
Кожен крок ізольований: збій одного не ламає решту.
"""
import glob
import os
import pwd
import shutil
import subprocess
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

_META = "http://metadata.google.internal/computeMetadata/v1/"
_META_KEYS = {"name": "instance/name", "machine_type": "instance/machine-type", "zone": "instance/zone",
              "id": "instance/id", "project_id": "project/project-id"}


def _redact(text: str) -> str:
    from gateway.ops_api import redact
    return redact(text)


def _run(cmd: List[str], timeout: float = 6) -> Optional[List[str]]:
    if not shutil.which(cmd[0]):
        return None
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except Exception as e:  # noqa: BLE001
        return [f"error: {type(e).__name__}"]
    text = out.stdout if out.stdout.strip() else (out.stderr or "")
    return [_redact(l.rstrip()) for l in text.splitlines() if l.strip()][:200]


def _metadata() -> Dict[str, Any]:
    res: Dict[str, Any] = {}
    for k, path in _META_KEYS.items():
        try:
            req = urllib.request.Request(_META + path, headers={"Metadata-Flavor": "Google"})
            with urllib.request.urlopen(req, timeout=2) as r:
                v = r.read().decode().strip()
            res[k] = v.rsplit("/", 1)[-1] if k in ("machine_type", "zone") else v
            if k in ("machine_type", "zone"):
                res[k + "_full"] = v
        except Exception as e:  # noqa: BLE001
            res[k] = f"unavailable ({type(e).__name__})"
    return res


_META_EXTRA = {"cpu_platform": "instance/cpu-platform", "preemptible": "instance/scheduling/preemptible",
               "automatic_restart": "instance/scheduling/automatic-restart",
               "on_host_maintenance": "instance/scheduling/on-host-maintenance",
               "tags": "instance/tags", "sa_email": "instance/service-accounts/default/email",
               "sa_scopes": "instance/service-accounts/default/scopes",
               "net_ifaces": "instance/network-interfaces/?recursive=true",
               "disks": "instance/disks/?recursive=true", "hostname": "instance/hostname"}


def _meta_get(path: str) -> Optional[str]:
    try:
        req = urllib.request.Request(_META + path, headers={"Metadata-Flavor": "Google"})
        with urllib.request.urlopen(req, timeout=2) as r:
            return r.read().decode().strip()
    except Exception:  # noqa: BLE001
        return None


def _metadata_extra() -> Dict[str, Any]:
    import json
    res: Dict[str, Any] = {}
    for k, path in _META_EXTRA.items():
        v = _meta_get(path)
        if v is None:
            res[k] = "unavailable"
        elif k == "net_ifaces":
            try:
                res[k] = [{"network": i.get("network", "").rsplit("/", 1)[-1], "ip": i.get("ip"),
                           "access_configs": [{"type": a.get("type"), "external_ip": a.get("externalIp"),
                                               "network_tier": a.get("networkTier")} for a in i.get("accessConfigs", [])]}
                          for i in json.loads(v)]
            except Exception:  # noqa: BLE001
                res[k] = "parse_error"
        elif k == "disks":
            try:
                res[k] = [{kk: d.get(kk) for kk in ("deviceName", "index", "mode", "type", "interface")} for d in json.loads(v)]
            except Exception:  # noqa: BLE001
                res[k] = "parse_error"
        elif k == "sa_scopes":
            res[k] = v.split()
        else:
            res[k] = _redact(v)
    return res


def _gcp_readonly(project: str, scopes: List[str]) -> Dict[str, Any]:
    """Лише GET list-запити до Compute API токеном метадата-сервера. Токен ніколи не виводиться/не логується."""
    import json
    ok_scope = any(x.endswith("/cloud-platform") or x.endswith("/compute") or x.endswith("/compute.readonly") for x in scopes)
    if not ok_scope:
        return {"status": "skipped: token has no compute/cloud-platform scope"}
    tok = _meta_get("instance/service-accounts/default/token")
    if not tok:
        return {"status": "skipped: token unavailable"}
    try:
        access = json.loads(tok)["access_token"]
    except Exception:  # noqa: BLE001
        return {"status": "skipped: token parse error"}
    base = f"https://compute.googleapis.com/compute/v1/projects/{project}/"
    calls = {"instances": "aggregated/instances", "disks": "aggregated/disks", "addresses": "aggregated/addresses",
             "reservations": "aggregated/reservations", "commitments": "aggregated/commitments",
             "snapshots": "global/snapshots", "images": "global/images?filter=" + "NOT%20name%3A%22%22"}
    calls.pop("images")
    res: Dict[str, Any] = {"status": "ok"}
    for name, path in calls.items():
        try:
            req = urllib.request.Request(base + path, headers={"Authorization": "Bearer " + access})
            with urllib.request.urlopen(req, timeout=8) as r:
                data = json.loads(r.read().decode())
            items: List[Any] = []
            if "items" in data and isinstance(data["items"], dict):
                for scope_name, v in data["items"].items():
                    for it in v.get(name, []):
                        items.append((scope_name.rsplit("/", 1)[-1], it))
            else:
                items = [("global", it) for it in data.get("items", [])]
            out = []
            for sc, it in items:
                row = {"scope": sc, "name": it.get("name")}
                for kk in ("machineType", "status", "sizeGb", "type", "address", "addressType", "status", "networkTier",
                           "diskSizeGb", "storageBytes", "plan", "category", "endTimestamp", "creationTimestamp"):
                    if kk in it:
                        row[kk] = str(it[kk]).rsplit("/", 1)[-1]
                if name == "instances":
                    row["scheduling"] = {kk: it.get("scheduling", {}).get(kk) for kk in ("preemptible", "provisioningModel")}
                    row["nat_ips"] = [a.get("natIP") for n in it.get("networkInterfaces", []) for a in n.get("accessConfigs", [])]
                out.append(row)
            res[name] = out
        except urllib.error.HTTPError as e:
            res[name] = f"HTTP {e.code}"
        except Exception as e:  # noqa: BLE001
            res[name] = f"error: {type(e).__name__}"
    return res


def _os_release() -> str:
    try:
        d = dict(l.strip().split("=", 1) for l in open("/etc/os-release") if "=" in l)
        return d.get("PRETTY_NAME", "").strip('"')
    except OSError:
        return "unknown"


def _crontabs() -> Dict[str, Any]:
    res: Dict[str, Any] = {}
    cur = pwd.getpwuid(os.getuid()).pw_name
    users = {cur, "root"}
    try:
        for l in open("/etc/passwd"):
            p = l.split(":")
            if len(p) > 6 and (p[2].isdigit() and int(p[2]) >= 1000) and not p[6].strip().endswith(("nologin", "false")):
                users.add(p[0])
    except OSError:
        pass
    for u in sorted(users):
        cmd = ["crontab", "-l"] if u == cur else ["crontab", "-u", u, "-l"]
        out = _run(cmd)
        if out is not None:
            res[u] = [l for l in out if not l.startswith("#")] or out[:3]
    res["_etc_cron.d"] = sorted(os.path.basename(p) for p in glob.glob("/etc/cron.d/*"))
    for d in ("hourly", "daily", "weekly", "monthly"):
        res[f"_etc_cron.{d}"] = sorted(os.path.basename(p) for p in glob.glob(f"/etc/cron.{d}/*"))
    try:
        res["_etc_crontab"] = [_redact(l.strip()) for l in open("/etc/crontab") if l.strip() and not l.startswith("#")]
    except OSError:
        pass
    return res


def _ports() -> Any:
    out = _run(["ss", "-ltnpH"])
    if out is None:
        return "ss not installed"
    res = []
    for l in out:
        parts = l.split()
        if len(parts) >= 4:
            procs = ""
            if "users:" in l:
                import re
                procs = ",".join(sorted(set(re.findall(r'\(\("([^"]+)"', l) + re.findall(r',\("([^"]+)"', l))))
            res.append(f"{parts[3]} {procs}".strip())
    return res


def host_snapshot() -> Dict[str, Any]:
    h: Dict[str, Any] = {"gce": _metadata()}
    h["gce_extra"] = _metadata_extra()
    scopes = h["gce_extra"].get("sa_scopes")
    pid = h["gce"].get("project_id", "")
    h["gcp_readonly"] = (_gcp_readonly(pid, scopes) if isinstance(scopes, list) and pid and not pid.startswith("unavailable")
                         else {"status": "skipped: no metadata"})
    h["lscpu"] = [l for l in (_run(["lscpu"]) or []) if l.split(":")[0] in ("Architecture", "CPU(s)", "Model name", "Thread(s) per core", "CPU MHz", "Hypervisor vendor")]
    h["block_devices"] = _run(["lsblk", "-o", "NAME,SIZE,TYPE,MOUNTPOINT"])
    h["nproc"] = (_run(["nproc"]) or ["?"])[0]
    h["memory_mb"] = _run(["free", "-m"])
    h["swap"] = _run(["swapon", "--show"]) or "none/unknown"
    h["disk_root"] = _run(["df", "-h", "/"])
    h["uptime"] = (_run(["uptime", "-p"]) or ["?"])[0]
    h["load"] = open("/proc/loadavg").read().strip() if os.path.exists("/proc/loadavg") else "?"
    h["os"] = _os_release()
    h["kernel"] = (_run(["uname", "-r"]) or ["?"])[0]
    h["services_running"] = _run(["systemctl", "list-units", "--type=service", "--state=running", "--no-legend", "--no-pager"])
    h["timers"] = _run(["systemctl", "list-timers", "--all", "--no-legend", "--no-pager"])
    h["cron"] = _crontabs()
    docker = _run(["docker", "ps", "--format", "{{.Names}}\t{{.Image}}\t{{.Status}}"])
    h["docker"] = "not installed" if docker is None else docker
    # лише імена процесів (comm), без аргументів
    h["top_mem_processes"] = _run(["ps", "-eo", "comm,rss,pmem", "--sort=-rss", "--no-headers"])[:8] if shutil.which("ps") else None
    h["listening_ports"] = _ports()
    mem = {}
    try:
        for l in open("/proc/meminfo"):
            k, v = l.split(":", 1)
            if k in ("MemTotal", "MemAvailable", "SwapTotal", "SwapFree"):
                mem[k] = int(v.split()[0]) // 1024
        h["meminfo_mb"] = mem
    except OSError:
        pass
    return h
