#!/usr/bin/env python3
"""Host diagnostics for the IICT InfoSec Lab server (runs on the host, no root needed).

Writes ~/infoseclab-site/data/diag/report.json, which the website's admin
Maintenance tab shows and sends to Gemini.

  collect.py                 run now
  collect.py --if-requested  run only if the admin pressed "Run diagnostic" (cron, every minute)
"""
import json, os, re, shutil, socket, subprocess, sys, time
from datetime import datetime, timezone

OUT_DIR = os.path.expanduser("~/infoseclab-site/data/diag")
REQUEST = os.path.join(OUT_DIR, "request")
DOMAIN = "lab.example.edu"
REMOTE = "--remote-json" in sys.argv          # running on gpubox (IICTLab Station 2), piped in over SSH
REMOTE_DIRS = ["/data/docker", "/srv/iictlab-homes", "/data"]
GPUBOX = "gpubox"                             # ~/.ssh/config alias


def sh(cmd, timeout=30):
    try:
        r = subprocess.run(cmd, shell=isinstance(cmd, str), capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip()
    except Exception as e:
        return f"ERROR: {e}"


def read(path, default=""):
    try:
        with open(path) as f:
            return f.read().strip()
    except Exception:
        return default


def safe(fn):
    try:
        return fn()
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------- sections

def host():
    osr = dict(re.findall(r'^(\w+)="?([^"\n]*)"?', read("/etc/os-release"), re.M))
    up = float(read("/proc/uptime").split()[0])
    reboot = os.path.exists("/var/run/reboot-required")
    return {
        "hostname": socket.gethostname(),
        "os": osr.get("PRETTY_NAME"),
        "kernel_running": os.uname().release,
        "kernels_installed": sorted(re.findall(r"vmlinuz-(\S+)", " ".join(os.listdir("/boot")))),
        "uptime_days": round(up / 86400, 2),
        "reboot_required": reboot,
        "reboot_required_by": read("/var/run/reboot-required.pkgs").split() if reboot else [],
        "time_synced": sh("timedatectl show -p NTPSynchronized --value") == "yes",
        "timezone": sh("timedatectl show -p Timezone --value"),
    }


def cpu():
    def snap():
        v = list(map(int, read("/proc/stat").splitlines()[0].split()[1:]))
        return sum(v), v[3] + v[4]
    t1, i1 = snap(); time.sleep(1); t2, i2 = snap()
    model = re.search(r"model name\s*:\s*(.+)", read("/proc/cpuinfo"))
    load = read("/proc/loadavg").split()[:3]
    return {
        "model": model.group(1) if model else None,
        "cores": os.cpu_count(),
        "usage_pct": round(100 * (1 - (i2 - i1) / max(t2 - t1, 1)), 1),
        "load_1_5_15": list(map(float, load)),
        "load_per_core_5m": round(float(load[1]) / os.cpu_count(), 2),
    }


def memory():
    m = {k: int(v) * 1024 for k, v in re.findall(r"^(\w+):\s+(\d+)", read("/proc/meminfo"), re.M)}
    gb = lambda b: round(b / 2**30, 2)
    return {
        "total_gb": gb(m["MemTotal"]), "available_gb": gb(m["MemAvailable"]),
        "used_pct": round(100 * (1 - m["MemAvailable"] / m["MemTotal"]), 1),
        "swap_total_gb": gb(m["SwapTotal"]), "swap_used_gb": gb(m["SwapTotal"] - m["SwapFree"]),
        "oom_kills_7d": sh("journalctl -k --since -7d --no-pager -q | grep -ci 'out of memory'") or "0",
    }


def disks():
    fs = []
    for line in sh("df -PT -x tmpfs -x devtmpfs -x overlay -x squashfs -x efivarfs").splitlines()[1:]:
        dev, typ, size, used, avail, pct, mnt = line.split(None, 6)
        st = os.statvfs(mnt)
        fs.append({"mount": mnt, "device": dev, "type": typ, "size_gb": round(int(size) / 2**20, 1),
                   "used_gb": round(int(used) / 2**20, 1), "used_pct": int(pct.rstrip("%")),
                   "inodes_used_pct": round(100 * (1 - st.f_ffree / st.f_files), 1) if st.f_files else None})
    devs = []
    for d in os.listdir("/sys/block"):
        if d.startswith(("loop", "ram", "dm-")):
            continue
        base = f"/sys/block/{d}/device"
        devs.append({"name": d, "model": read(f"{base}/model"), "firmware": read(f"{base}/firmware_rev"),
                     "size_gb": round(int(read(f"/sys/block/{d}/size", "0")) * 512 / 1e9, 1)})
    smart = sh("sudo -n /usr/sbin/smartctl -H -A /dev/nvme0n1 2>&1")
    if "password" in smart or "sudo:" in smart:
        smart = "Permission denied"
    return {"filesystems": fs, "devices": devs,
            "smart": smart if not any(x in smart for x in ("Permission denied", "ERROR", "not found")) else "unavailable (needs root; install smartmontools)",
            "big_dirs_home_gb": {p: round(int(sh(f"du -sb {p} 2>/dev/null | cut -f1") or 0) / 2**30, 2)
                                 for p in (REMOTE_DIRS if REMOTE else [os.path.expanduser("~/iictlab/data"),
                                           os.path.expanduser("~/paperbrowser/data"), os.path.expanduser("~/infoseclab-site/data")])}}


def temperatures():
    out = {}
    for h in os.listdir("/sys/class/hwmon"):
        base = f"/sys/class/hwmon/{h}"
        name = read(f"{base}/name", h)
        for f in os.listdir(base):
            if f.startswith("temp") and f.endswith("_input"):
                label = read(f"{base}/{f.replace('_input', '_label')}", f.split("_")[0])
                out[f"{name}/{label}"] = int(read(f"{base}/{f}", "0")) / 1000
    return out


def gpu():
    q = "name,driver_version,temperature.gpu,utilization.gpu,memory.used,memory.total,power.draw,power.limit,fan.speed,pstate"
    rows = sh(f"nvidia-smi --query-gpu={q} --format=csv,noheader,nounits")
    if not rows or rows.startswith("ERROR"):
        return {"error": rows or "nvidia-smi gave no output"}
    gpus = [dict(zip(q.split(","), [x.strip() for x in r.split(",")])) for r in rows.splitlines()]
    cuda = re.search(r"CUDA Version:\s*([\d.]+)", sh("nvidia-smi"))
    procs = sh("nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv,noheader")
    return {"gpus": gpus, "cuda_driver_version": cuda.group(1) if cuda else None, "processes": procs.splitlines()}


def network():
    ifaces = []
    for i in json.loads(sh("ip -j addr") or "[]"):
        n = i["ifname"]
        if n == "lo" or n.startswith(("veth", "br-", "docker")):
            continue
        st = lambda k: int(read(f"/sys/class/net/{n}/statistics/{k}", "0"))
        ifaces.append({"name": n, "state": i.get("operstate"), "physical": os.path.exists(f"/sys/class/net/{n}/device"),
                       "speed_mbps": read(f"/sys/class/net/{n}/speed") or None,
                       "addresses": [a["local"] for a in i.get("addr_info", [])],
                       "rx_gb": round(st("rx_bytes") / 2**30, 2), "tx_gb": round(st("tx_bytes") / 2**30, 2),
                       "errors": st("rx_errors") + st("tx_errors"), "crc_errors": st("rx_crc_errors"),
                       "missed_ring_full": st("rx_missed_errors"), "rx_packets": st("rx_packets"),
                       "dropped_by_kernel": st("rx_dropped") + st("tx_dropped"),
                       "dropped_note": "kernel drops of unwanted frames (unknown protocol/VLAN/multicast on the LAN); "
                                       "not a cabling fault unless crc_errors/errors grow"})
    ports = sorted({l.split()[3] for l in sh("ss -tlnH").splitlines() if l.split()})
    return {"interfaces": ifaces, "listening_tcp": ports,
            "internet_ok": sh("curl -s -o /dev/null -w '%{http_code}' -m 8 https://www.google.com") in ("200", "301", "302")}


def tls():
    out = sh(f"echo | openssl s_client -servername {DOMAIN} -connect {DOMAIN}:443 2>/dev/null "
             "| openssl x509 -noout -enddate -issuer")
    m = re.search(r"notAfter=(.+)", out)
    if not m:
        return {"error": "could not read certificate"}
    end = datetime.strptime(m.group(1).strip(), "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
    return {"domain": DOMAIN, "expires": end.isoformat(), "days_left": (end - datetime.now(timezone.utc)).days,
            "issuer": (re.search(r"issuer=(.+)", out) or [None, None])[1]}


def docker():
    cs = []
    ids = sh("docker ps -aq").split()
    if ids:
        for c in json.loads(sh(["docker", "inspect", *ids]) or "[]"):
            st = c["State"]
            cs.append({"name": c["Name"].lstrip("/"), "image": c["Config"]["Image"], "state": st["Status"],
                       "health": (st.get("Health") or {}).get("Status"), "restarts": c["RestartCount"],
                       "started": st["StartedAt"][:19], "oom_killed": st.get("OOMKilled")})
    stats = {}
    for l in sh("docker stats --no-stream --format '{{json .}}'", timeout=60).splitlines():
        s = json.loads(l)
        stats[s["Name"]] = {"cpu": s["CPUPerc"], "mem": s["MemUsage"], "mem_pct": s["MemPerc"]}
    for c in cs:
        c.update(stats.get(c["name"], {}))
    images = [json.loads(l) for l in sh("docker images --format '{{json .}}'").splitlines()]
    return {"version": sh("docker version --format '{{.Server.Version}}'"),
            "compose_version": sh("docker compose version --short"),
            "containers": cs,
            "images_in_use": sorted({c["image"] for c in cs}),
            "image_ages": {f'{i["Repository"]}:{i["Tag"]}': i["CreatedSince"] for i in images if i["Tag"] != "<none>"},
            "dangling_images": sum(1 for i in images if i["Tag"] == "<none>"),
            "disk_usage": sh("docker system df --format '{{.Type}}: {{.Size}} (reclaimable {{.Reclaimable}})'").splitlines()}


def updates():
    counts = sh("/usr/lib/update-notifier/apt-check 2>&1")  # "regular;security"
    reg, sec = (counts.split(";") + ["?", "?"])[:2] if ";" in counts else ("?", "?")
    lst = [l.split("/")[0] for l in sh("apt list --upgradable 2>/dev/null").splitlines()[1:]]
    last = sh("stat -c %y /var/lib/apt/periodic/update-success-stamp 2>/dev/null")[:16]
    return {"apt_upgradable": int(reg) if reg.isdigit() else reg, "apt_security": int(sec) if sec.isdigit() else sec,
            "packages": lst[:60], "last_apt_update": last or None,
            "unattended_upgrades": sh("systemctl is-enabled unattended-upgrades 2>/dev/null"),
            "nvidia_driver_packages": [p for p in lst if "nvidia" in p]}


def services():
    failed = [l.split()[1] if l.startswith("●") else l.split()[0]
              for l in sh("systemctl --failed --no-legend --plain").splitlines() if l.strip()]
    key = {u: sh(f"systemctl is-active {u}") for u in
           ["docker", "ssh", "cron", "systemd-timesyncd", "unattended-upgrades", "nvidia-persistenced", "ufw", "fail2ban"]}
    crontab = [l for l in sh("crontab -l 2>/dev/null").splitlines() if l and not l.startswith("#")]
    return {"failed_units": failed, "key_units": key, "user_crontab": crontab}


def logs():
    errs = sh("journalctl -p err --since -24h --no-pager -q -o cat 2>/dev/null")
    lines = errs.splitlines()
    ssh_fail = sh("journalctl -u ssh --since -24h --no-pager -q 2>/dev/null | grep -cE 'Failed password|Invalid user'")
    return {"errors_24h": len(lines), "recent_errors": lines[-20:],
            "ssh_failed_logins_24h": int(ssh_fail) if ssh_fail.isdigit() else ssh_fail,
            "ssh_password_auth": sh("grep -hiE '^\\s*PasswordAuthentication' /etc/ssh/sshd_config /etc/ssh/sshd_config.d/*.conf 2>/dev/null")}


def apps():
    get = lambda u: sh(f"curl -s -m 8 -o /dev/null -w '%{{http_code}} %{{time_total}}' {u}")
    lab = sh(f"curl -s -m 8 https://{DOMAIN}/lab/services/booking/status.json")
    return {"http": {"iictleaf_login": get(f"https://{DOMAIN}/login"), "website": get(f"https://{DOMAIN}/"),
                     "iictlab_hub": get(f"https://{DOMAIN}/lab/hub/health"), "paper_browser": get(f"https://{DOMAIN}/papers/")},
            "iictlab_booking": json.loads(lab) if lab.startswith("{") else lab,
            "paperbrowser_downloads_mb": round(int(sh(f"du -sb {os.path.expanduser('~/paperbrowser/data/downloads')} 2>/dev/null | cut -f1") or 0) / 2**20, 1)}


HOST_SECTIONS = ["host", "cpu", "memory", "disks", "temperatures", "gpu", "network", "docker", "updates", "services", "logs"]


def gpubox():
    """Station 2: run this same script on gpubox over SSH (host sections only), plus the tunnel's state here."""
    me = os.path.abspath(__file__)
    try:
        with open(me) as f:
            r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", GPUBOX, "python3", "-", "--remote-json"],
                               stdin=f, capture_output=True, text=True, timeout=180)
        out = json.loads(r.stdout) if r.stdout.strip().startswith("{") else {"error": (r.stderr or "no output")[-300:]}
    except Exception as e:
        out = {"error": f"unreachable: {e}"}
    t = sh("docker inspect -f '{{.State.Status}} restarts={{.RestartCount}} since={{.State.StartedAt}}' iictlab-gpubox-tunnel")
    out["tunnel"] = {"container": t, "reverse_hub_port_on_gpubox": sh(f"ssh -o BatchMode=yes -o ConnectTimeout=10 {GPUBOX} "
                                                                       "'curl -s -m 8 -o /dev/null -w %{http_code} http://127.0.0.1:18081/lab/hub/api/'") == "200"}  # a real request, not a listener count
    lab = sh(f"curl -s -m 8 https://{DOMAIN}/lab/services/booking/status.json")
    try:
        out["station"] = next(x for x in json.loads(lab)["stations"] if x["station"] == 2)
    except Exception:
        out["station"] = None
    return out


def main():
    if REMOTE:
        print(json.dumps({n: safe(globals()[n]) for n in HOST_SECTIONS}, default=str))
        return
    os.makedirs(OUT_DIR, exist_ok=True)
    prog = os.path.join(OUT_DIR, "progress")
    if os.path.exists(prog) and time.time() - os.path.getmtime(prog) < 300:
        return  # another run is still going (the website shows its progress)
    if "--if-requested" in sys.argv:
        if not os.path.exists(REQUEST):
            return
        os.remove(REQUEST)
    t0 = time.time()
    report = {"collected_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    steps = [("host", host), ("cpu", cpu), ("memory", memory), ("disks", disks), ("temperatures", temperatures),
             ("gpu", gpu), ("network", network), ("tls", tls), ("docker", docker), ("updates", updates),
             ("services", services), ("logs", logs), ("apps", apps), ("gpubox", gpubox)]
    try:
        for i, (name, fn) in enumerate(steps):
            with open(prog, "w") as f:  # read by the Maintenance page
                json.dump({"step": name, "i": i, "of": len(steps), "started": t0}, f)
            report[name] = safe(fn)
        report["took_s"] = round(time.time() - t0, 1)
        tmp = os.path.join(OUT_DIR, "report.json.tmp")
        with open(tmp, "w") as f:
            json.dump(report, f, indent=1, default=str)
        os.replace(tmp, os.path.join(OUT_DIR, "report.json"))
    finally:
        try:
            os.remove(prog)
        except OSError:
            pass

if __name__ == "__main__":
    main()
