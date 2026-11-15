# Lab knowledge (fixed facts; trust these over guesses)

## Machines

* S1 = main server "": i7-14700, 15 GB RAM, RTX 3060 12 GB, 1 TB NVMe. Public IP <PUBLIC\_IP> on eno1.
Runs everything: IICTLeaf (Overleaf fork), IICTLab (JupyterHub), the website, Paper Browser, nginx, certbot.
* S2 = "gpubox" (Dell R760xs): Xeon Silver 4410T 40 threads, 31 GB RAM, NVIDIA A2 16 GB. Disks behind a PERC RAID
controller, so no SMART data there (normal). Its hostname is "user" and timezone Asia/Makassar (harmless).
/data on S2 holds the admin's research experiments : never suggest deleting,
moving or wiping anything under 
S2 also runs the Ollama container (this AI); S2 load average is high for a few minutes after an AI run. Normal. It may later be used for a T-Pot honeypot experiment.

## How they connect

* S1 reaches S2 only over the LAN through the container iictlab-gpubox-tunnel (SSH, port <SSH\_PORT>). Nothing on S2 is
exposed to the internet. Ollama listens on 127.0.0.1 on S2 only.
* S2's USB network adapter (enx…, 100 Mb/s) carries the tunnel IP <S2\_LAN\_IP>; it was added on purpose.
The onboard eno8303 (1 Gb/s) also exists. A 100 Mb/s link on the USB adapter is known, not a fault.
* "error: connect\_to 127.0.0.1 port 18888: failed" in S2 logs = the tunnel probing a notebook that is not running. Normal.

## IICTLab bookings

* One student per station at a time, up to 7 days (+3). Station modes: on / draining (no new bookings) / off.
* Never recommend a reboot, docker restart or driver update of a station while it is booked; give the date it frees up.
* Containers named iictlab-nb-\* are student notebooks, project-\* are IICTLeaf compile jobs, pb-\* are Paper Browser
sessions: these come and go; stopped ones are normal.

## Known normal things

* Kernel "dropped" packets on eno1 with 0 CRC errors = unwanted LAN broadcast frames, not a cable fault.
* sudo "a password is required" lines in logs come from the diagnostic collector itself. Harmless.
* postfix is disabled on S2 on purpose (no mail there). The \*-wait-online units failing on S2 at boot are harmless.
* Updates: unattended-upgrades installs security updates automatically on S1. NVIDIA driver updates need a reboot
and a GPU test afterwards.
* fail2ban protects SSH on both machines.
* Reclaimable Docker image space is mostly old notebook images; prune only when disk > 70%.

## Expected baseline (anything different from this IS a finding)

* Ports open on all addresses: S1 only 22, 80, 443. S2 only <SSH\_PORT>, plus 172.17.0.1:18081 (relay so Station 2 notebooks on Docker's bridge reach the hub; expected).
Any other port on 0.0.0.0 or \[::] is an exposure:
critical if it is a Docker API (2375/2376), a database, or Ollama (11434).
* Swap: normally 0 GB used on both. More than half the swap used = memory pressure (students' jobs), even if RAM looks fine now.
* S1 SSD "unsafe shutdowns" was 4 in Nov 2026. A higher number means power cuts or hard resets since; with a short
uptime it points to power loss (check UPS / ask about outages).
* unattended-upgrades must be active and enabled on both, and "last apt update" should be within 2 days. Off or stale = warning.
* GPU idle temperature 25–55 °C; over 85 °C is a problem.
* A short uptime alone is not a finding (admins reboot for updates); only together with more unsafe shutdowns.

