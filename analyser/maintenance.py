"""Admin > Maintenance: host diagnostics (collected on the host by ~/maintenance/collect.py)
plus advice from Gemini, with a rule-based fallback when the API is unavailable."""
import json
import os
import re
import urllib.request
from datetime import datetime, timezone

DIAG = "/data/diag"
REPORT = os.path.join(DIAG, "report.json")
ANALYSIS = os.path.join(DIAG, "analysis.json")
REQUEST = os.path.join(DIAG, "request")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODELS = [m.strip() for m in os.environ.get("GEMINI_MODEL", "gemma-4-26b-a4b-it,gemma-4-31b-it").split(",") if m.strip()]
GEMINI_MODEL = GEMINI_MODELS[0]


def load(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def request_run():
    os.makedirs(DIAG, exist_ok=True)
    with open(REQUEST, "w") as f:
        f.write(datetime.now(timezone.utc).isoformat())


def pending():
    return os.path.exists(REQUEST)


# ---------------------------------------------------------------- rule-based checks (also the fallback)

def _num(x, default=0.0):
    try:
        return float(re.sub(r"[^\d.]", "", str(x)) or default)
    except ValueError:
        return default


def rules(r, where=""):
    """[(severity, area, finding, action)] — severity: critical | warning | ok.
    where="gpubox": the same checks on Station 2's section (areas prefixed, commands run there)."""
    out = []
    pre = f"{where}: " if where else ""
    add = lambda sev, area, what, do="": out.append({"severity": sev, "area": pre + area, "finding": what,
                                                     "action": (f"on {where}: {do}" if where and do else do)})
    if not where and isinstance(r.get("gpubox"), dict):
        g = r["gpubox"]
        if "error" in g and len(g) <= 3:
            add("critical", "Station 2", "gpubox could not be checked: " + str(g["error"])[:150],
                "ssh gpubox; docker logs iictlab-gpubox-tunnel")
        else:
            out.extend(x for x in rules(g, "gpubox") if x["severity"] != "ok" or x["area"].endswith("Updates"))
        t = g.get("tunnel") or {}
        if not str(t.get("container", "")).startswith("running") or t.get("reverse_hub_port_on_gpubox") is False:
            add("critical", "Station 2", f"Tunnel to gpubox not healthy ({t}).", "docker restart iictlab-gpubox-tunnel; docker logs iictlab-gpubox-tunnel")
        st = g.get("station") or {}
        if st.get("mode") and st["mode"] != "on":
            add("ok", "Station 2", f"Station 2 is {st['mode']} (no new bookings).", "")

    for fs in (r.get("disks") or {}).get("filesystems", []):
        p = fs.get("used_pct", 0)
        if p >= 90:
            add("critical", "Disk", f"{fs['mount']} is {p}% full.", "Free space now: docker builder prune, remove old images/archives.")
        elif p >= 80:
            add("warning", "Disk", f"{fs['mount']} is {p}% full.", "Plan a cleanup (docker system df, ~/iictlab/data/archive).")
        if (fs.get("inodes_used_pct") or 0) >= 85:
            add("warning", "Disk", f"{fs['mount']} inodes {fs['inodes_used_pct']}% used.", "Find folders with many small files.")
    smart = str((r.get("disks") or {}).get("smart", ""))
    if where:
        pass  # gpubox's disks sit behind a Dell PERC RAID controller: no direct SMART
    elif "unavailable" in smart:
        add("warning", "Disk", "SSD health (SMART) not checked: needs root.", "Run: sudo smartctl -a /dev/nvme0n1")
    else:
        g = lambda k: _num((re.search(rf"{k}:\s*([\d,x]+)", smart) or [0, "0"])[1].replace(",", "").replace("0x", ""))
        if "PASSED" not in smart:
            add("critical", "Disk", "SSD SMART self-assessment did not pass.", "Back up now; plan SSD replacement.")
        if g("Critical Warning"):
            add("critical", "Disk", "SSD reports a critical warning.", "sudo smartctl -a /dev/nvme0n1")
        if g("Media and Data Integrity Errors"):
            add("critical", "Disk", f"SSD media errors: {g('Media and Data Integrity Errors'):.0f}.", "Back up now; plan SSD replacement.")
        if g("Percentage Used") >= 80:
            add("warning", "Disk", f"SSD wear {g('Percentage Used'):.0f}%.", "Plan SSD replacement.")

    m = r.get("memory") or {}
    if m.get("used_pct", 0) >= 90:
        add("critical", "Memory", f"RAM {m['used_pct']}% used.", "Check docker stats for the biggest container.")
    if _num(m.get("oom_kills_7d")) > 0:
        add("warning", "Memory", f"{m['oom_kills_7d']} out-of-memory kills in 7 days.", "Lower per-student memory limit or add RAM/swap.")
    if m.get("swap_total_gb", 1) == 0:
        add("warning", "Memory", "No swap configured.", "Consider a small swap file as an OOM cushion.")

    c = r.get("cpu") or {}
    if c.get("load_per_core_5m", 0) >= 1:
        add("warning", "CPU", f"5-minute load per core is {c['load_per_core_5m']}.", "See which container is busy (docker stats).")

    temps = r.get("temperatures") or {}
    if temps:
        hot = max(temps.values())
        if hot >= 90:
            add("critical", "Temperature", f"Hottest sensor {hot} °C.", "Check cooling and airflow now.")
        elif hot >= 80:
            add("warning", "Temperature", f"Hottest sensor {hot} °C.", "Watch cooling under load.")

    for g in (r.get("gpu") or {}).get("gpus", []):
        t = _num(g.get("temperature.gpu"))
        if t >= 85:
            add("warning", "GPU", f"{g['name']} at {t:.0f} °C.", "Check GPU fans/airflow.")
    if "error" in (r.get("gpu") or {}):
        add("critical", "GPU", "nvidia-smi failed: " + str(r["gpu"]["error"])[:120], "Driver may be broken after an update; reboot or reinstall the driver.")

    h = r.get("host") or {}
    if h.get("reboot_required"):
        add("warning", "Updates", "A reboot is required (" + ", ".join(h.get("reboot_required_by", [])[:5]) + ").",
            "Reboot in a quiet hour, when no IICTLab slot is active.")
    if h.get("time_synced") is False:
        add("warning", "System", "Clock is not NTP-synchronised.", "Check systemd-timesyncd.")

    u = r.get("updates") or {}
    if isinstance(u.get("apt_security"), int) and u["apt_security"] > 0:
        add("critical", "Updates", f"{u['apt_security']} security updates pending.", "sudo apt update && sudo apt upgrade")
    elif isinstance(u.get("apt_upgradable"), int) and u["apt_upgradable"] > 0:
        add("ok", "Updates", f"{u['apt_upgradable']} normal package updates available.", "Apply at the next maintenance window.")
    if u.get("nvidia_driver_packages"):
        add("warning", "Updates", "NVIDIA driver packages in the update list.", "Upgrade them together with a reboot; test GPU after.")

    t = r.get("tls") or {}
    if "days_left" in t:
        if t["days_left"] < 7:
            add("critical", "TLS", f"Certificate expires in {t['days_left']} days.", "Check certbot renewal (iictlab-certbot).")
        elif t["days_left"] < 20:
            add("warning", "TLS", f"Certificate expires in {t['days_left']} days.", "Renewal should run soon; check certbot logs.")

    for cn in (r.get("docker") or {}).get("containers", []):
        if cn.get("state") != "running" and not cn["name"].startswith(("iictlab-nb-", "pb-", "project-")):
            add("critical", "Docker", f"{cn['name']} is {cn.get('state')}.", f"docker start {cn['name']} and read its logs.")
        if cn.get("health") == "unhealthy":
            add("critical", "Docker", f"{cn['name']} is unhealthy.", f"docker logs --tail 100 {cn['name']}")
        if (cn.get("restarts") or 0) > 3:
            add("warning", "Docker", f"{cn['name']} restarted {cn['restarts']} times.", "Look for a crash loop in its logs.")
        if cn.get("oom_killed"):
            add("warning", "Docker", f"{cn['name']} was killed for memory.", "Raise its memory limit or find the leak.")

    s = r.get("services") or {}
    if s.get("failed_units"):
        add("warning", "System", "Failed systemd units: " + ", ".join(s["failed_units"]), "systemctl status <unit>")
    if (s.get("key_units") or {}).get("unattended-upgrades") not in ("active",):
        add("warning", "Updates", "Automatic security updates are off.", "sudo apt install unattended-upgrades && sudo dpkg-reconfigure unattended-upgrades")

    lg = r.get("logs") or {}
    if _num(lg.get("ssh_failed_logins_24h")) > 50:
        add("warning", "Security", f"{lg['ssh_failed_logins_24h']} failed SSH logins in 24 h.", "Run ~/golive/security-fixes.sh --no-password; consider fail2ban.")
    if "yes" in str(lg.get("ssh_password_auth", "")).lower():
        add("warning", "Security", "SSH password login is enabled.", "Use keys only: ~/golive/security-fixes.sh --no-password")

    for name, res in ((r.get("apps") or {}).get("http") or {}).items():
        code = str(res).split()[0] if res else ""
        if not code or code[0] not in "23":
            add("critical", "Services", f"{name} answered {res or 'nothing'}.", "Check that service's container and nginx.")

    for i in (r.get("network") or {}).get("interfaces", []):
        if i.get("physical") and i.get("state") == "UP" and i.get("speed_mbps") and _num(i["speed_mbps"]) < 1000:
            add("warning", "Network", f"{i['name']} linked at {i['speed_mbps']} Mb/s.", "Check cable/switch port.")
        if i.get("errors", 0) > 100:
            add("warning", "Network", f"{i['name']} has {i['errors']} errors.", "Check cable.")
    if (r.get("network") or {}).get("internet_ok") is False:
        add("critical", "Network", "No internet access from the server.", "Check gateway/DNS.")

    if not where and not any(x["severity"] != "ok" for x in out):
        add("ok", "Overall", "No problems found by the built-in checks.", "")
    order = {"critical": 0, "warning": 1, "ok": 2}
    return sorted(out, key=lambda x: order[x["severity"]])


# ---------------------------------------------------------------- Gemini

# ---------------------------------------------------------------- what the AI sees: a short fact list, not the raw report
KB_FILE = os.path.join(os.path.dirname(__file__), "kb.md")
ACCEPTED = os.path.join(DIAG, "accepted.txt")  # admin-editable on the Maintenance page


def kb():
    try:
        return open(KB_FILE).read().strip()
    except OSError:
        return ""


def accepted():
    try:
        return open(ACCEPTED).read().strip()
    except OSError:
        return ""


def _ago(ts, now):
    """'2026-11-14 11:57' -> '0.9 days ago' (small models cannot do date maths)."""
    try:
        t = datetime.fromisoformat(str(ts).replace(" ", "T"))
        t = t if t.tzinfo else t.replace(tzinfo=timezone.utc)
        d = max(0.0, (now - t).total_seconds() / 86400)  # S2 logs local time
        return f"{d:.1f} days ago ({'fresh' if d <= 2 else 'STALE, over 2 days'})"
    except Exception:
        return str(ts)


def _in(ts, now):
    try:
        return f"{(datetime.fromisoformat(str(ts)) - now).total_seconds() / 86400:.1f} days from now ({str(ts)[:10]})"
    except Exception:
        return str(ts)


UNSAFE_BASELINE = int(os.environ.get("SSD_UNSAFE_BASELINE", "4"))  # S1 SSD count in Nov 2026


def _host_facts(r, tag, now=None):
    """One short line per fact that can change a decision. Normal noise (every container, every core) is left out."""
    f = []
    h, c, m = r.get("host") or {}, r.get("cpu") or {}, r.get("memory") or {}
    f.append(f"{tag} up {h.get('uptime_days')} days, kernel {h.get('kernel_running')}, reboot "
             f"{'REQUIRED by ' + ', '.join(h.get('reboot_required_by', [])[:4]) if h.get('reboot_required') else 'not needed'}"
             f"{'' if h.get('time_synced', True) else ', clock NOT synced'}")
    f.append(f"{tag} CPU {c.get('usage_pct')}% now, load 1/5/15 min {c.get('load_1_5_15')}, {c.get('cores')} threads")
    f.append(f"{tag} RAM {m.get('used_pct')}% used of {m.get('total_gb')} GB, swap {m.get('swap_used_gb')}/{m.get('swap_total_gb')} GB"
             f"{' (OVER HALF used)' if (m.get('swap_used_gb') or 0) > (m.get('swap_total_gb') or 1) / 2 else ''}, "
             f"OOM kills 7d: {m.get('oom_kills_7d')}")
    for fs in (r.get("disks") or {}).get("filesystems", []):
        if fs.get("size_gb", 0) >= 5:
            f.append(f"{tag} disk {fs['mount']} {fs.get('used_pct')}% used ({fs.get('used_gb')}/{fs.get('size_gb')} GB)")
    smart = str((r.get("disks") or {}).get("smart", ""))
    if smart and "unavailable" not in smart and tag == "S1":
        pick = lambda k: (re.search(rf"{k}:\s*(\S+)", smart) or [0, "?"])[1]
        f.append(f"{tag} SSD SMART {'PASSED' if 'PASSED' in smart else 'NOT PASSED'}, wear {pick('Percentage Used')}, "
                 f"media errors {pick('Media and Data Integrity Errors')}, unsafe shutdowns {pick('Unsafe Shutdowns')}"
                 f" ({'same as' if pick('Unsafe Shutdowns') == str(UNSAFE_BASELINE) else 'CHANGED from'} baseline {UNSAFE_BASELINE})")
    t = r.get("temperatures") or {}
    if t:
        f.append(f"{tag} hottest sensor {max(t.values())} °C")
    for g in (r.get("gpu") or {}).get("gpus", []):
        f.append(f"{tag} GPU {g.get('name')} {g.get('temperature.gpu')} °C, util {g.get('utilization.gpu')}%, "
                 f"mem {g.get('memory.used')}/{g.get('memory.total')} MiB, driver {g.get('driver_version')}")
    if "error" in (r.get("gpu") or {}):
        f.append(f"{tag} GPU nvidia-smi FAILED: {str(r['gpu']['error'])[:100]}")
    for i in (r.get("network") or {}).get("interfaces", []):
        if i.get("physical") and (i.get("state") == "UP" or i.get("addresses")):
            f.append(f"{tag} NIC {i['name']} {i.get('state')} {i.get('speed_mbps')} Mb/s, errors {i.get('errors')}, "
                     f"crc {i.get('crc_errors')}, addr {','.join(a for a in i.get('addresses', []) if ':' not in a) or '-'}")
    n = r.get("network") or {}
    if n.get("internet_ok") is False:
        f.append(f"{tag} NO internet access")
    if n.get("listening_tcp"):
        pub = [p for p in n["listening_tcp"] if not p.startswith(("127.", "[::1]"))]
        f.append(f"{tag} ports open on all addresses: {', '.join(pub)}")
    u = r.get("updates") or {}
    f.append(f"{tag} updates: {u.get('apt_upgradable')} packages ({u.get('apt_security')} security)"
             f"{': ' + ', '.join(u.get('packages', [])[:8]) if u.get('packages') else ''}"
             f"{', NVIDIA: ' + ', '.join(u['nvidia_driver_packages'][:4]) if u.get('nvidia_driver_packages') else ''}"
             f", unattended-upgrades {u.get('unattended_upgrades')}, last apt update {_ago(u.get('last_apt_update'), now)}")
    s = r.get("services") or {}
    if s.get("failed_units"):
        f.append(f"{tag} failed systemd units: {', '.join(s['failed_units'])}")
    ku = s.get("key_units") or {}
    if ku:
        f.append(f"{tag} services: " + ", ".join(f"{k} {v}" for k, v in ku.items()))
    d = r.get("docker") or {}
    cs = d.get("containers", [])
    if cs:
        eph = ("iictlab-nb-", "pb-", "project-")
        bad = [f"{x['name']} {x.get('state')}" + (f" restarts={x['restarts']}" if (x.get("restarts") or 0) > 3 else "")
               + (" OOM" if x.get("oom_killed") else "") + (" UNHEALTHY" if x.get("health") == "unhealthy" else "")
               for x in cs if (x.get("state") != "running" and not x["name"].startswith(eph))
               or (x.get("restarts") or 0) > 3 or x.get("oom_killed") or x.get("health") == "unhealthy"]
        f.append(f"{tag} containers {sum(x.get('state') == 'running' for x in cs)}/{len(cs)} running; "
                 f"problems: {'; '.join(bad) if bad else 'none'}")
    if d.get("disk_usage"):
        f.append(f"{tag} docker {d['disk_usage'][0]}")
    lg = r.get("logs") or {}
    if lg:
        errs = [e[:120] for e in lg.get("recent_errors", []) if e and "a password is required" not in e]
        uniq = list(dict.fromkeys(errs))[:5]
        f.append(f"{tag} log errors 24h: {lg.get('errors_24h')}, failed SSH logins 24h: {lg.get('ssh_failed_logins_24h')}"
                 f"{', SSH password auth ' + str(lg['ssh_password_auth']) if lg.get('ssh_password_auth') else ''}")
        f += [f"{tag} log: {e}" for e in uniq]
    return f


def facts(r):
    """[(id, text)] — the only data the model gets. IDs let us check its claims afterwards."""
    try:
        now = datetime.fromisoformat(r["collected_at"])
    except Exception:
        now = datetime.now(timezone.utc)
    f = _host_facts(r, "S1", now)
    t = r.get("tls") or {}
    if t:
        f.append(f"S1 TLS certificate {t.get('domain')} expires in {t.get('days_left')} days")
    a = r.get("apps") or {}
    if a.get("http"):
        f.append("S1 web checks: " + ", ".join(f"{k} {str(v).split()[0] if v else 'NO ANSWER'}" for k, v in a["http"].items()))
    for s in (a.get("iictlab_booking") or {}).get("stations", []):
        f.append(f"S{s['station']} booking: mode {s.get('mode')}, "
                 + (f"in use by {s.get('user')}, free again {_in(s.get('next_free'), now)}" if s.get("in_use") else "free now"))
    g = r.get("gpubox")
    if isinstance(g, dict):
        if g.get("host"):
            f += _host_facts(g, "S2", now)
            tu = g.get("tunnel") or {}
            f.append(f"S2 tunnel container {str(tu.get('container', '?')).split(' since')[0]}, "
                     f"reverse hub port {'ok' if tu.get('reverse_hub_port_on_gpubox') else 'DOWN'}")
        else:
            f.append(f"S2 NOT REACHABLE: {str(g.get('error'))[:150]}")
    f.append(f"report time {r.get('collected_at')} (now)")
    return [(f"F{i}", x) for i, x in enumerate(f, 1)]


PROMPT = """You review the IICT BUET InfoSec Lab servers for the admins. Use ONLY the knowledge and facts below.
If something is not in them, you do not know it: do not guess, and do not report it.

KNOWLEDGE (fixed, always true):
{kb}

ALREADY ACCEPTED BY THE ADMINS (never report these again):
{accepted}

BUILT-IN CHECK RESULTS (already shown to the admin; do not repeat them, but use them):
{rules}

FACTS (current measurement):
{facts}

Task: find at most 4 things that need judgement: risks, odd combinations, and timing (e.g. when to reboot given the
bookings). Skip anything normal according to KNOWLEDGE. Fewer items is better; zero is fine.
Every item must list the fact IDs (F..) it is based on. Keep each field to one sentence.

First, in "review", compare the facts with the "Expected baseline" in KNOWLEDGE, one baseline line at a time,
and note every fact that differs from it, is risky, or contradicts KNOWLEDGE as "F..: why" (max 8 short notes).
Something is only "normal" if KNOWLEDGE says so explicitly.
Then write items only from those notes.

Answer with JSON only:
{{"review": ["F..: why"], "summary": "one sentence", "health": "good|attention|critical",
 "items": [{{"severity": "critical|warning|ok", "area": "short", "finding": "...", "action": "exact command or step", "facts": ["F1"]}}]}}"""


def build_prompt(report, found):
    fs = facts(report)
    rl = "\n".join(f"- [{x['severity']}] {x['area']}: {x['finding']}" for x in found if x["severity"] != "ok") or "- nothing"
    return PROMPT.format(kb=kb(), accepted=accepted() or "- nothing yet", rules=rl,
                         facts="\n".join(f"{i}: {t}" for i, t in fs)), {i for i, _ in fs}


def _check(out, ids, found=()):
    """Keep only items backed by real fact IDs (drops invented findings). Records what was dropped."""
    if not isinstance(out.get("items"), list):
        raise ValueError("unexpected answer format")
    keep, dropped = [], []
    for it in out["items"]:
        if not isinstance(it, dict):
            continue
        cited = [str(x).strip() for x in (it.get("facts") or [])]
        if cited and all(x in ids for x in cited) and it.get("severity") in ("critical", "warning", "ok"):
            it["facts"] = cited
            keep.append(it)
        else:
            dropped.append(it.get("finding", "")[:100])
    out["items"] = keep[:4]
    out["dropped"] = dropped
    if out.get("health") not in ("good", "attention", "critical"):
        out["health"] = "attention" if keep else "good"
    rank = {"good": 0, "attention": 1, "critical": 2}
    floor = max([{"critical": 2, "warning": 1}.get(x["severity"], 0) for x in found] + [0])
    own = max([{"critical": 2, "warning": 1}.get(x["severity"], 0) for x in keep] + [0])
    if rank[out["health"]] < own:  # its own critical item means critical health
        out["health"] = ["good", "attention", "critical"][own]
    if rank[out["health"]] < floor:  # never rate health better than the built-in checks found
        out["health"] = ["good", "attention", "critical"][floor]
        worst = next(x for x in found if {"critical": 2, "warning": 1}.get(x["severity"], 0) == floor)
        out["summary"] = f"Built-in checks: {worst['area']}: {worst['finding']}"  # the model's "all fine" would contradict it
    return out


def rule_updates(report, found):
    """The update table comes from facts, not the model."""
    out = []
    for tag, r in (("S1", report), ("S2", report.get("gpubox") or {})):
        u, h = r.get("updates") or {}, r.get("host") or {}
        if not u:
            continue
        st = next((s for s in ((report.get("apps") or {}).get("iictlab_booking") or {}).get("stations", [])
                   if f"S{s['station']}" == tag), {})
        busy = f" after {str(st.get('next_free'))[:10]} (booked)" if st.get("in_use") else ""
        if u.get("nvidia_driver_packages"):
            out.append({"what": f"{tag} NVIDIA driver", "recommendation": "update in maintenance window",
                        "why": f"needs a reboot and a GPU test{busy}"})
        if (u.get("apt_security") or 0) > 0:
            out.append({"what": f"{tag} {u['apt_security']} security updates", "recommendation": "update now" if not busy else "update in maintenance window",
                        "why": f"security fixes{busy}"})
        elif (u.get("apt_upgradable") or 0) > 0:
            out.append({"what": f"{tag} {u['apt_upgradable']} packages", "recommendation": "update in maintenance window", "why": "no security fixes among them"})
        if h.get("reboot_required"):
            out.append({"what": f"{tag} reboot", "recommendation": "update in maintenance window" if busy else "update now",
                        "why": f"required by {', '.join(h.get('reboot_required_by', [])[:3])}{busy}"})
    return out


OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://iictlab-gpubox-tunnel:11434")  # gpubox, over the LAN tunnel
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "gemma4:12b")


def ask_ollama(report, found):
    """Local model on Station 2 (gpubox), over the LAN tunnel."""
    prompt, ids = build_prompt(report, found)
    body = {"model": OLLAMA_MODEL, "stream": False, "format": "json", "think": False,  # thinking runs away on gemma4:12b
            "messages": [{"role": "user", "content": prompt}],
            "options": {"temperature": 0.1, "num_ctx": 8192, "num_predict": 1500}}
    req = urllib.request.Request(f"{OLLAMA_URL}/api/chat", data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as resp:
        return _check(_json_from(json.load(resp)["message"]["content"]), ids, found)


def ask_gemini(report, found):
    """Try each model in GEMINI_MODEL (comma list) until one answers."""
    errors = []
    tried = []
    for model in GEMINI_MODELS + discover_models():
        if model in tried:
            continue
        tried.append(model)
        if len(tried) > 6:
            break
        try:
            return _ask(model, report, found), model
        except Exception as e:
            errors.append(f"{model}: {getattr(e, 'code', '')} {e}")
    raise RuntimeError("; ".join(errors))


def discover_models():
    """Fallback when the configured models are gone: free-tier-friendly models the key can use right now."""
    try:
        req = urllib.request.Request("https://generativelanguage.googleapis.com/v1beta/models?pageSize=200",
                                     headers={"x-goog-api-key": GEMINI_KEY})
        with urllib.request.urlopen(req, timeout=20) as resp:
            names = [m["name"].split("/", 1)[1] for m in json.load(resp).get("models", [])
                     if "generateContent" in m.get("supportedGenerationMethods", [])]
    except Exception:
        return []
    bad = ("tts", "image", "embed", "audio", "live", "omni", "lyria", "veo", "imagen", "banana", "robotics", "computer")
    names = [n for n in names if not any(b in n for b in bad)]
    rank = lambda n: (0 if n.startswith("gemma") else 1 if "flash-lite" in n else 2 if "flash" in n else 3,
                      "preview" in n, n)
    return sorted(names, key=rank)


def _json_from(text):
    text = text.strip()
    m = re.search(r"\{.*\}", text, re.S)  # Gemma may wrap JSON in prose or ``` fences
    return json.loads(m.group(0) if m else text)


def _ask(model, report, found):
    if not GEMINI_KEY:
        raise RuntimeError("no GEMINI_API_KEY set")
    prompt, ids = build_prompt(report, found)
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.1, "maxOutputTokens": 2048}}
    if not model.startswith("gemma"):  # Gemma has no JSON mode
        body["generationConfig"]["responseMimeType"] = "application/json"
    req = urllib.request.Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json", "x-goog-api-key": GEMINI_KEY})
    with urllib.request.urlopen(req, timeout=90) as resp:
        data = json.load(resp)
    parts = data["candidates"][0]["content"]["parts"]
    text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
    return _check(_json_from(text), ids, found)


def analyse(report):
    """Local model on Station 2 first, then Gemini, then the built-in checks. Cached per report."""
    found = rules(report)
    result = {"for": report.get("collected_at"), "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "rules": found, "updates": rule_updates(report, found)}
    errors = []
    _state.update(stage="Asking the local model on Station 2")
    try:
        result.update(ask_ollama(report, found), source=f"Local model on Station 2 ({OLLAMA_MODEL})")
        _save(result)
        return result
    except Exception as e:
        errors.append(f"local: {e}")
    _state.update(stage="Local model unavailable, asking Gemini")
    try:
        answer, model = ask_gemini(report, found)
        result.update(answer, source=f"Gemini ({model}); local model unavailable")
    except Exception as e:
        e = RuntimeError("; ".join(errors + [f"gemini: {e}"]))
        crit = sum(x["severity"] == "critical" for x in found)
        warn = sum(x["severity"] == "warning" for x in found)
        result.update(source="Built-in checks (AI unavailable)", ai_error=str(e)[:300],
                      health="critical" if crit else "attention" if warn else "good",
                      summary=f"{crit} critical, {warn} warnings from the built-in checks.",
                      items=found)
    _save(result)
    return result


def _save(result):
    os.makedirs(DIAG, exist_ok=True)
    with open(ANALYSIS, "w") as f:
        json.dump(result, f, indent=1)


# ---------------------------------------------------------------- background run (the web request returns at once)
import threading
_busy = threading.Lock()


def analysing():
    return _busy.locked()


_state = {"stage": "", "since": 0}
PROGRESS = os.path.join(DIAG, "progress")
AUTO = os.path.join(DIAG, "auto_analyse")  # "Run diagnostic" also analyses the new report


def analyse_async(report):
    if not _busy.acquire(blocking=False):
        return False
    import time
    _state.update(stage="Starting", since=time.time(), error="")
    def work():
        try:
            analyse(report)
        except Exception as e:
            _state["error"] = str(e)[:200]
        finally:
            _busy.release()
    threading.Thread(target=work, daemon=True).start()
    return True


def status():
    """What the Maintenance page polls every 2 s."""
    import time
    now = time.time()
    rep = load(REPORT) or {}
    ana = load(ANALYSIS) or {}
    out = {"collected_at": rep.get("collected_at"), "analysed_for": ana.get("for"), "error": _state.get("error", "")}
    prog = load(PROGRESS)
    if prog and now - os.path.getmtime(PROGRESS) < 300:
        return {**out, "phase": "collecting", "step": prog["step"], "i": prog["i"], "of": prog["of"],
                "elapsed": round(now - prog["started"])}
    if os.path.exists(REQUEST):
        age = round(now - os.path.getmtime(REQUEST))
        if age > 150:  # cron checks every minute; 2.5 min means it is not picking up
            os.remove(REQUEST)
            return {**out, "phase": "failed", "error": "The host collector did not start (is the cron job running?)."}
        return {**out, "phase": "queued", "elapsed": age}
    if analysing():
        return {**out, "phase": "analysing", "step": _state["stage"], "elapsed": round(now - _state["since"])}
    if os.path.exists(AUTO) and rep and rep.get("collected_at") != ana.get("for"):
        os.remove(AUTO)
        analyse_async(rep)
        return {**out, "phase": "analysing", "step": "Starting", "elapsed": 0}
    if os.path.exists(AUTO) and now - os.path.getmtime(AUTO) > 600:
        os.remove(AUTO)
    return {**out, "phase": "idle"}
