"""Monthly system report emailed to every IICTLeaf admin.

Runs inside the website container (restart: always), so it survives reboots and updates.
A thread checks every 30 min; from 08:00 (Dhaka) on the 1st, the month's report is sent once.
If the server was down on the 1st, it is sent as soon as the site is back (catch-up).
The month is recorded in /data/diag/monthly_sent before sending, so it never goes out twice.
"""
import html
import json
import logging
import os
import smtplib
import threading
import time
import zoneinfo
from datetime import datetime
from email.message import EmailMessage

import maintenance as mt

log = logging.getLogger("monthly")
TZ = zoneinfo.ZoneInfo("Asia/Dhaka")
SENT = os.path.join(mt.DIAG, "monthly_sent")
SEND_HOUR = 8
_lock = threading.Lock()


def fresh_report(max_wait=240):
    """Ask the host collector for a new report and wait for it (the host cron checks every minute)."""
    before = (mt.load(mt.REPORT) or {}).get("collected_at")
    mt.request_run()
    for _ in range(max_wait // 10):
        time.sleep(10)
        r = mt.load(mt.REPORT)
        if r and r.get("collected_at") != before:
            return r
    log.warning("no fresh report from the host collector; using the last one")
    return mt.load(mt.REPORT)


def _stats(r):
    g = (r.get("gpu") or {}).get("gpus") or [{}]
    g = g[0]
    root = next((f for f in (r.get("disks") or {}).get("filesystems", []) if f["mount"] == "/"), {})
    temps = r.get("temperatures") or {}
    cs = (r.get("docker") or {}).get("containers", [])
    smart = str((r.get("disks") or {}).get("smart", ""))
    wear = next((l.split(":")[1].strip() for l in smart.splitlines() if l.startswith("Percentage Used")), "?")
    u, h = r.get("updates") or {}, r.get("host") or {}
    return [
        ("Uptime", f"{h.get('uptime_days')} days · kernel {h.get('kernel_running')}"),
        ("CPU", f"{(r.get('cpu') or {}).get('usage_pct')}% now · load {' / '.join(map(str, (r.get('cpu') or {}).get('load_1_5_15', [])))}"),
        ("Memory", f"{(r.get('memory') or {}).get('used_pct')}% used of {(r.get('memory') or {}).get('total_gb')} GB"),
        ("Disk /", f"{root.get('used_pct')}% used ({root.get('used_gb')} of {root.get('size_gb')} GB)"),
        ("SSD health", f"{'PASSED' if 'PASSED' in smart else 'not checked' if 'unavailable' in smart else 'CHECK'} · wear {wear}"),
        ("Hottest sensor", f"{max(temps.values()) if temps else '?'} °C"),
        ("GPU", f"{g.get('name', '?')} · {g.get('temperature.gpu', '?')} °C · driver {g.get('driver_version', '?')}"),
        ("Containers", f"{sum(c.get('state') == 'running' for c in cs)}/{len(cs)} running"),
        ("Updates", f"{u.get('apt_upgradable')} packages ({u.get('apt_security')} security) · reboot {'REQUIRED' if h.get('reboot_required') else 'not needed'}"),
        ("TLS certificate", f"{(r.get('tls') or {}).get('days_left', '?')} days left"),
        ("Log errors (24 h)", f"{(r.get('logs') or {}).get('errors_24h')} · {(r.get('logs') or {}).get('ssh_failed_logins_24h')} failed SSH logins"),
    ]


def build(r, a, test=False):
    month = datetime.now(TZ).strftime("%B %Y")
    subject = f"{'[TEST] ' if test else ''}InfoSec Lab server: monthly report, {month} — {a.get('health', '?').upper()}"
    stats = _stats(r)
    g = r.get("gpubox") or {}
    if g.get("host"):
        gs = dict(_stats(g))
        st = g.get("station") or {}
        data = next((f for f in (g.get("disks") or {}).get("filesystems", []) if f["mount"] == "/data"), {})
        stats += [("— Station 2 (gpubox)", f"{st.get('mode', '?')} · {'in use by ' + str(st.get('user')) if st.get('in_use') else 'free'}"
                   f" · tunnel {'ok' if (g.get('tunnel') or {}).get('reverse_hub_port_on_gpubox') else 'DOWN'}"),
                  ("gpubox CPU / RAM", f"{gs['CPU']} · {gs['Memory']}"),
                  ("gpubox GPU", gs["GPU"]),
                  ("gpubox disks", f"/ {gs['Disk /']} · /data {data.get('used_pct')}% ({data.get('used_gb')} of {data.get('size_gb')} GB)"),
                  ("gpubox updates", gs["Updates"])]
    elif g:
        stats.append(("— Station 2 (gpubox)", "NOT REACHABLE: " + str(g.get("error"))[:120]))
    todos = [i for i in a.get("items", []) if i.get("severity") != "ok"]
    checks = [i for i in a.get("rules", []) if i.get("severity") != "ok"] if "ai_error" not in a else []
    ups = a.get("updates", [])
    url = "https://lab.example.edu/site/admin/maintenance"

    # plain text
    t = [subject, "", a.get("summary", ""), "", "SYSTEM"]
    t += [f"  {k:<18} {v}" for k, v in stats]
    t += ["", "TO DO"] + ([f"  [{i['severity']}] {i['area']}: {i['finding']}\n      -> {i['action']}" for i in todos] or ["  Nothing urgent."])
    if checks:
        t += ["", "BUILT-IN CHECKS"] + [f"  [{i['severity']}] {i['area']}: {i['finding']}" for i in checks]
    if ups:
        t += ["", "UPDATES"] + [f"  {u['what']}: {u['recommendation']} ({u['why']})" for u in ups]
    t += ["", f"Analysis: {a.get('source')}. Report collected {r.get('collected_at')} UTC.", f"Details: {url}"]

    # html
    e = html.escape
    color = {"critical": "#A32035", "warning": "#b7791f", "ok": "#2f7d4f", "good": "#2f7d4f", "attention": "#b7791f"}
    badge = lambda s: f'<span style="color:{color.get(s, "#666")};font-weight:600;text-transform:uppercase;font-size:12px">{e(s)}</span>'
    rows = "".join(f'<tr><td style="padding:4px 12px 4px 0;color:#666565">{e(k)}</td><td style="padding:4px 0">{e(str(v))}</td></tr>' for k, v in stats)
    todo = "".join(f'<li style="margin-bottom:8px">{badge(i["severity"])} <b>{e(i["area"])}</b>: {e(i["finding"])}'
                   f'<br><code style="font-size:12px;background:#f4f2f2;padding:2px 4px">{e(i["action"])}</code></li>' for i in todos) or "<li>Nothing urgent.</li>"
    chk = "".join(f'<li>{badge(i["severity"])} {e(i["area"])}: {e(i["finding"])}</li>' for i in checks)
    upd = "".join(f'<tr><td style="padding:4px 12px 4px 0">{e(u["what"])}</td><td style="padding:4px 12px 4px 0"><b>{e(u["recommendation"])}</b></td>'
                  f'<td style="padding:4px 0;color:#666565">{e(u["why"])}</td></tr>' for u in ups)
    body = f"""<div style="font-family:Segoe UI,Arial,sans-serif;color:#1E1D22;max-width:680px;font-size:14px;line-height:1.5">
<h2 style="color:#A32035;margin:0 0 4px">InfoSec Lab server — {e(month)}</h2>
<p style="margin:0 0 16px">{badge(a.get('health', '?'))} {e(a.get('summary', ''))}</p>
<h3 style="margin:16px 0 6px">To do</h3><ul style="padding-left:18px;margin:0">{todo}</ul>
{f'<h3 style="margin:16px 0 6px">Updates</h3><table style="border-collapse:collapse">{upd}</table>' if upd else ''}
<h3 style="margin:16px 0 6px">System</h3><table style="border-collapse:collapse">{rows}</table>
{f'<h3 style="margin:16px 0 6px">Built-in checks</h3><ul style="padding-left:18px;margin:0">{chk}</ul>' if chk else ''}
<p style="margin-top:20px;color:#666565;font-size:12px">Analysis: {e(a.get('source', ''))}. Collected {e(r.get('collected_at', ''))} UTC.
<a href="{url}" style="color:#A32035">Open Maintenance</a></p></div>"""
    return subject, "\n".join(t), body


def send(recipients, subject, text, body):
    user, pw = os.environ.get("SMTP_USER", ""), os.environ.get("OVERLEAF_EMAIL_SMTP_PASS", "")
    if not (user and pw):
        raise RuntimeError("SMTP not configured")
    with smtplib.SMTP_SSL(os.environ.get("SMTP_HOST", "smtp.gmail.com"), 465, timeout=30) as smtp:
        smtp.login(user, pw)
        for to in recipients:  # one mail each: admins don't see each other's addresses
            msg = EmailMessage()
            msg["Subject"], msg["From"], msg["To"] = subject, f"InfoSec Lab server <{user}>", to
            msg.set_content(text)
            msg.add_alternative(body, subtype="html")
            smtp.send_message(msg)


def run(recipients, test=False):
    r = fresh_report()
    if not r:
        raise RuntimeError("no diagnostic report available")
    a = mt.analyse(r)
    send(recipients, *build(r, a, test=test))
    log.info("monthly report sent to %s (test=%s, %s)", recipients, test, a.get("source"))
    return a


def _due():
    now = datetime.now(TZ)
    month = now.strftime("%Y-%m")
    try:
        last = open(SENT).read().strip()
    except OSError:
        last = ""
    if last >= month:
        return None
    if now.day == 1 and now.hour < SEND_HOUR:
        return None
    return month


def loop(admin_emails):
    time.sleep(120)  # let the site settle after a restart
    while True:
        try:
            with _lock:
                month = _due()
                if month:
                    os.makedirs(mt.DIAG, exist_ok=True)
                    with open(SENT, "w") as f:  # mark first: never send twice, even if a send half-fails
                        f.write(month)
                    run(admin_emails())
        except Exception:
            log.exception("monthly report failed")
        time.sleep(1800)


def start(admin_emails):
    os.makedirs(mt.DIAG, exist_ok=True)
    if not os.path.exists(SENT):  # first install: start with next month's report
        with open(SENT, "w") as f:
            f.write(datetime.now(TZ).strftime("%Y-%m"))
    threading.Thread(target=loop, args=(admin_emails,), daemon=True).start()
