# Metaculus MiniBench bot — senguyen72-bot

Baseline forecasting bot for the Metaculus MiniBench tournament series
($1,000 per 2-week round, bot-only). Account `senguyen72`, first bot
`senguyen72-bot` (the only bot on the account eligible for prizes).

## Files

| File | What it is |
| --- | --- |
| `bot.py` | The bot (Python 3, **stdlib only** — no dependencies, no paid services, no LLM calls) |
| `research.py` | Research + calibration layer (v2): free news fetch (AskNews if keyed, else Google News RSS / GDELT), evidence scoring, optional OpenRouter LLM step |
| `config.json` | Account + `METACULUS_TOKEN` (chmod 600; **never commit, never paste in chat**). Optional keys live here too: `asknews_api_key`, `openrouter_api_key`, `openrouter_model` (same rule) |
| `state.json` | Submitted question ids (created on first real submit; committed so runs don't double-submit) |
| `.github/workflows/forecast.yml` | GitHub Actions: runs `bot.py --submit` every hour |
| `.gitignore` | Keeps `config.json` + raw dumps out of git |
| `openapi.yml` | Official Metaculus OpenAPI spec v2.0.0 (downloaded 23/11/2026) |
| `minibench_all_raw.json` | Saved `/api/posts/?tournaments=minibench` response (60 questions, round 16/11–04/12/2026) — fixture for offline dry-runs |
| `PROGRESS.md` | Progress log (Vietnamese) |

## Usage

```bash
python3 bot.py --selftest                                        # offline CDF + research checks
python3 bot.py --research-test                                   # live news fetch on 3 samples, never submits
python3 bot.py --dry-run                                          # live list, print forecasts, submit nothing
python3 bot.py --fixture minibench_all_raw.json --dry-run          # offline, real question shapes
python3 bot.py --submit --tournament minibench                    # submit for real
python3 bot.py --list-only                                        # just list open questions
python3 bot.py --dry-run --no-research                            # skip the news-research step
```

## How it forecasts (v2 — research + calibration, 27/11/2026)

Evidence order per question: **Community Prediction → LLM (if keys
configured) → news research (heuristic) → honest fallback.**

- **Anchor:** Community Prediction (CP), when the API returns it —
  binary shrunk 15% toward 0.5 (clamped [0.02, 0.98]); multiple-choice
  shrunk 15% toward uniform; continuous centred on the CP median.
- **Research (v2):** CP is `null` for bot accounts on MiniBench posts, so
  the bot fetches recent news for the question — AskNews first if an
  `ASKNEWS_API_KEY` is configured (free Metaculus-partnered access, see
  PROGRESS.md), otherwise free Google News RSS, with GDELT as a backup —
  and scores the evidence deterministically: binary cue-word votes
  (clamped to [0.20, 0.80], scaled by agreement, article count and cue
  volume), multiple-choice option-mention counts (Laplace-smoothed, shrunk
  30% toward uniform), numeric/discrete/date medians of numbers/dates
  extracted from article text (sd 25–32% of range with evidence, 40%
  without). No usable evidence → the honest fallback below.
- **Optional LLM step (dormant until keys exist):** with
  `OPENROUTER_API_KEY` + `OPENROUTER_MODEL` configured (donated Metaculus
  credits via OpenRouter), the model forecasts from the question + news
  summaries; its JSON is strictly validated and any failure falls back to
  the heuristic evidence. See PROGRESS.md for how to obtain the keys.
- **Fallback (the normal case when nothing else applies):** binary 0.5,
  multiple-choice uniform, numeric/discrete/date a broad normal CDF
  centred on the range midpoint (sd = 40% of range).
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
4. The workflow runs every hour and on manual dispatch.

## Status (27/11/2026)

- Bot v2 built: research + calibration (see PROGRESS.md §27/11). Selftest
  PASS, research-test PASS (live news), fixture dry-run on 60 real
  MiniBench questions PASS (all payloads build, 0 submitted), live
  dry-run 27/11: **0 open questions** in the current MiniBench round.
- **Bot v2 pushed (commit 2df54ec, 27/11/2026) and live:** `bot.py` + `research.py`
  (research + calibration) are on GitHub; the workflow now runs hourly. A
  manual Actions run after the push completed SUCCESS in 12s. The scheduled
  workflow will submit real forecasts when questions open.
- **0 open questions** in the current MiniBench round (16/11–04/12/2026):
  all 60 questions closed 16–18/11. Nothing to submit yet. The next
  round is expected ~30/11/2026 (bi-weekly; questions usually launch the
  Saturday before). The scheduled workflow will catch it when it opens.
- **Still needs Se (see PROGRESS.md §27/11 for exact steps):** (1) fill
  out the Metaculus participation form (required for all participants;
  also the application for donated OpenRouter LLM credits); (2) AskNews
  free access — create an account with senguyen72@gmail.com, then email
  contact@asknews.app or DM @freqai on Discord (note: the form asks for
  a LinkedIn profile and Se has none — say so in the message);
  (3) optionally request the Bot Benchmarking tier via the Data Needs
  form to get Community Prediction access.
- See `PROGRESS.md` for the full log, including the two verified API
  limitations (CP is null for bot accounts; `my_forecasts` absent from
  the list endpoint) and the VM egress flakiness to metaculus.com.
