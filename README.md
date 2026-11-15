# AI-assisted system report — 

It runs from the Maintenance tab of the lab website and as a monthly email to the admins.
**The AI runs locally on the LAN** (gemma4:12b on Ollama on S2), so no server data leaves the lab.
Gemini is only used as a fallback.

## Pipeline

```
collect.py (host, cron)  ──►  report.json  ──►  facts digest  ──►  LLM  ──►  grounding check  ──►  page / email
   raw measurements            ~18 KB          ~45 numbered       gemma4    drop unsupported
   S1 + S2 over SSH                            lines + KB         12b       claims, fix health
                         └──►  built-in rules (thresholds) ────────────────────────┘
```

1. **Collect** (`collector/collect.py`). This runs on the host, not in a container, because it needs real hardware access. It reads:
   - CPU, RAM, disks, and the SSD's SMART health (via a sudoers rule for `smartctl` only);
   - temperatures and the GPU (`nvidia-smi`);
   - NICs and listening ports, and the TLS certificate;
   - Docker containers, apt updates, systemd units, logs, app HTTP checks and booking state.

   It runs the same script on S2 over SSH (`--remote-json`) and checks the tunnel between S1 and S2.
   It is triggered by cron: daily, plus every minute if an admin pressed *Run*. It writes a progress file for the page.
2. **Rules** (`analyser/maintenance.py: rules()`). These are deterministic threshold checks (disk > 80%, SMART failed,
   security updates, cert < 20 days, …). They always run, and they are the fallback if no AI is available.
3. **Facts digest** (`facts()`). The raw report becomes about 45 short numbered lines (`F12: S1 swap 3.9/4.0 GB (OVER HALF used)`):
   - noise is removed: individual containers, per-core temperatures, harmless log lines;
   - **comparisons are pre-computed**: ages in days, "fresh/STALE", "CHANGED from baseline", "OVER HALF used".

   The prompt goes from about 10,400 tokens (raw JSON) to about 1,700.
4. **Knowledge base** (`analyser/kb.md`). Fixed facts about the lab are always sent in full: the architecture,
   what is known to be normal, data that must never be touched, booking rules, and an **expected baseline**
   (allowed ports, normal swap, the SSD's unsafe-shutdown count, …).
   Admins can also add an "accepted" list on the page, so the model stops repeating decided issues.
5. **Prompt** (`PROMPT`). The model is asked to:
   - first write a short `review` comparing the facts with the baseline (a scratchpad inside the JSON);
   - then give at most 4 items, each citing the fact IDs it relies on.

   JSON mode is on, temperature 0.1, and thinking is off (gemma4's thinking mode ran away and never finished).
6. **Grounding check** (`_check()`):
   - items citing no fact IDs, or IDs that don't exist, are dropped;
   - health is never rated better than the worst built-in check or the model's own worst item;
   - the update table is built from facts, not by the model.
7. **Fallback chain**: local Ollama → Gemini (a model list, plus auto-discovery if Google retires models) → rules only.
8. **Output**:
   - the Maintenance page (`web/`) with a progress bar that polls `status.json` and a "done" dialog;
   - `monthly.py` emails each admin on the 1st of the month.

## Files

| Path | What |
|---|---|
| `collector/collect.py` | Host data collector (S1 + S2) |
| `analyser/maintenance.py` | Rules, facts digest, prompt, Ollama/Gemini calls, grounding check, status |
| `analyser/kb.md` | Knowledge base and expected baseline |
| `analyser/monthly.py` | Monthly email to admins |
| `web/maintenance.html`, `web/routes_excerpt.py` | Admin page and Flask routes (part of a larger Flask site) |
| `tests/eval_ai.py`, `tests/eval_results.txt` | Scenario tests and results |
| `docs/example_prompt.txt` | A real prompt as sent to the model |

Environment: `OLLAMA_URL`, `OLLAMA_MODEL` (default gemma4:12b), `GEMINI_API_KEY`, `GEMINI_MODEL`,
and SMTP settings for the email. The code was extracted from the lab website, so it is not a standalone package.
