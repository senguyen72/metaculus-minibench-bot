# Metaculus MiniBench bot — senguyen72-bot

Baseline forecasting bot for the Metaculus MiniBench tournament series
($1,000 per 2-week round, bot-only). Account `senguyen72`, first bot
`senguyen72-bot` (the only bot on the account eligible for prizes).

## Files

| File | What it is |
| --- | --- |
| `bot.py` | The bot (Python 3, **stdlib only** — no dependencies, no paid services, no LLM calls) |
| `config.json` | Account + `METACULUS_TOKEN` (chmod 600; **never commit, never paste in chat**) |
| `state.json` | Submitted question ids (created on first real submit; committed so runs don't double-submit) |
| `.github/workflows/forecast.yml` | GitHub Actions: runs `bot.py --submit` every 6h |
| `.gitignore` | Keeps `config.json` + raw dumps out of git |
| `openapi.yml` | Official Metaculus OpenAPI spec v2.0.0 (downloaded 23/11/2026) |
| `minibench_all_raw.json` | Saved `/api/posts/?tournaments=minibench` response (60 questions, round 16/11–04/12/2026) — fixture for offline dry-runs |
| `PROGRESS.md` | Progress log (Vietnamese) |

## Usage

```bash
python3 bot.py --selftest                                        # offline CDF checks
python3 bot.py --dry-run                                          # live list, print forecasts, submit nothing
python3 bot.py --fixture minibench_all_raw.json --dry-run          # offline, real question shapes
python3 bot.py --submit --tournament minibench                    # submit for real
python3 bot.py --list-only                                        # just list open questions
```

## How it forecasts (baseline v1 — honest, uninformative where it has no edge)

- **Anchor:** Community Prediction (CP), when the API returns it —
  binary shrunk 15% toward 0.5 (clamped [0.02, 0.98]); multiple-choice
  shrunk 15% toward uniform; continuous centred on the CP median.
- **Fallback (the normal case — see PROGRESS.md):** CP is `null` for bot
  accounts on MiniBench posts, so the bot uses maximum-entropy fallbacks:
  binary 0.5, multiple-choice uniform, numeric/discrete/date a broad
  normal CDF centred on the range midpoint (sd = 40% of range).
- CDFs follow the official spec exactly (201 points, min step 0.01/200,
  max step 0.2, bound rules) and are validated before submitting; a bad
  CDF is never submitted.
- Already-forecast questions are skipped (checked via the post-detail
  endpoint — the list endpoint does not return `my_forecasts`).

## GitHub Actions setup (one time, by the repo owner)

1. Push this folder to GitHub (e.g. `senguyen72/metaculus-minibench-bot`).
   Do **not** include `config.json`.
2. Repo Settings → Secrets and variables → Actions → New repository secret:
   `METACULUS_TOKEN` = the bot token from `config.json`.
3. Settings → Actions → General → Workflow permissions → **Read and write**
   (so the workflow can commit `state.json` back).
4. The workflow runs every 6 hours and on manual dispatch.

## Status (23/11/2026)

- Bot built, selftest PASS, fixture dry-run on 60 real MiniBench
  questions PASS (all 4 types, all CDFs valid), forecast-endpoint
  verified live (POST to a closed question correctly returns 405).
- **0 open questions** in the current MiniBench round (16/11–04/12/2026):
  all 60 questions closed 16–18/11. Nothing to submit yet. The next
  round is expected ~30/11/2026 (bi-weekly; questions usually launch the
  Saturday before). The scheduled workflow will catch it when it opens.
- See `PROGRESS.md` for the full log, including the two verified API
  limitations (CP is null for bot accounts; `my_forecasts` absent from
  the list endpoint) and the VM egress flakiness to metaculus.com.
