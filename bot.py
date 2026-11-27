#!/usr/bin/env python3
"""
Metaculus MiniBench forecasting bot — v2 (stdlib only, no paid services).

Bot account: senguyen72-bot (first bot on the account — the only one eligible
for MiniBench prizes). Token is read from config.json (same directory) or the
METACULUS_TOKEN environment variable. The token is NEVER printed or logged.

Forecast logic (v2 — research + calibration; see research.py):
  * Anchor: the Community Prediction (CP), when the API returns it.
    - Binary:  p' = 0.5 + 0.85 * (p_cp - 0.5), clamped to [0.02, 0.98].
    - Multiple choice: shrink each category toward uniform by 15%,
      then renormalise to sum 1.
    - Numeric/discrete/date: build a normal CDF centred on the CP median
      (or CP mean if no median), sd = max(0.25 * range, CP-implied sd).
  * Research (new in v2): when CP is null (the normal case for bot
    accounts), the bot fetches recent news for the question (AskNews if an
    ASKNEWS_API_KEY is configured, else free Google News RSS, GDELT as a
    backup) and scores the evidence with deterministic heuristics:
    binary cue-word votes, MC option-mention counts, numbers/dates
    extracted from article text for continuous questions. The move away
    from 0.5 is deliberately small and evidence-scaled (binary is clamped
    to [0.20, 0.80]; continuous sd shrinks 0.40 -> 0.25 with strong
    evidence). No evidence -> the old honest fallback (0.5 / uniform /
    broad CDF). See research.py for the exact formulas.
  * Optional LLM step (dormant until keys exist): if OPENROUTER_API_KEY
    and OPENROUTER_MODEL are configured (donated Metaculus credits via
    OpenRouter), the model forecasts from the question + news summaries;
    its JSON output is strictly validated, and any failure falls back to
    the heuristic evidence. See PROGRESS.md for how Se obtains the keys.
  * Fallback when CP is null AND there is no usable evidence:
    - Binary: 0.5 (maximum entropy — we claim no edge).
    - Multiple choice: uniform over the options.
    - Numeric/discrete/date: broad normal CDF centred on the range midpoint
      (geometric midpoint for log-scaled questions), sd = 0.40 of the range.
  * Questions already forecast (my_forecasts non-empty) are skipped.
  * Group/conditional posts: each subquestion is forecast separately by its
    own question id.

CDF rules (from the official OpenAPI spec, docs/openapi.yml):
  * 201 points for normal questions, inbound_outcome_count + 1 for discrete.
  * Min step 0.01 / inbound_outcome_count (0.00005 for 200); max step
    0.2 * (200 / inbound_outcome_count).
  * Closed lower bound -> cdf[0] == 0.0; open lower -> cdf[0] >= 0.001.
  * Closed upper bound -> cdf[-1] == 1.0; open upper -> cdf[-1] <= 0.999.
  * The standardize_cdf() below is a stdlib port of the spec's version.

Usage:
  python3 bot.py --dry-run [--tournament minibench]   # print, submit nothing
  python3 bot.py --submit                             # submit for real
  python3 bot.py --selftest                           # offline CDF + research checks
  python3 bot.py --list-only                          # list open questions
  python3 bot.py --research-test                      # live news fetch on 3 samples
  python3 bot.py --no-research                        # disable the research step
State is kept in state.json (submitted question ids + payload summaries)
so re-runs never re-submit the same question.
"""

import argparse
import json
import math
import os
import signal
import sys
import time
import urllib.error
import urllib.request

import research

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = "https://www.metaculus.com/api"
TOURNAMENT = "minibench"
STATE_FILE = os.path.join(HERE, "state.json")

# ---------------------------------------------------------------- API client

def load_token():
    tok = os.environ.get("METACULUS_TOKEN")
    if tok:
        return tok.strip()
    with open(os.path.join(HERE, "config.json")) as f:
        return json.load(f)["metaculus_token"].strip()


def load_optional_keys():
    """Optional research keys (AskNews / OpenRouter). Env vars win over
    config.json. Returns a dict of present keys only — values are NEVER
    printed or logged anywhere in this program."""
    keys = {}
    try:
        with open(os.path.join(HERE, "config.json")) as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        cfg = {}
    for env_name, cfg_name in (
            ("ASKNEWS_API_KEY", "asknews_api_key"),
            ("OPENROUTER_API_KEY", "openrouter_api_key"),
            ("OPENROUTER_MODEL", "openrouter_model")):
        v = os.environ.get(env_name) or cfg.get(cfg_name)
        if v:
            keys[cfg_name] = str(v).strip()
    return keys


class _HardTimeout(Exception):
    pass


def _alarm_handler(signum, frame):
    raise _HardTimeout("hard timeout (SIGALRM)")


def urlopen_hard(req, timeout=25):
    """urlopen with a SIGALRM hard timeout.

    Needed because the VM's egress to metaculus.com intermittently stalls
    inside the TLS read, where the socket timeout alone does not rescue the
    call promptly (observed live 23/11/2026: a call stuck >4 min despite
    timeout=25). SIGALRM bounds every attempt to ~`timeout` seconds.
    Main thread only; falls back to plain urlopen where SIGALRM is absent.
    """
    if not hasattr(signal, "SIGALRM"):
        return urllib.request.urlopen(req, timeout=timeout)
    old = signal.signal(signal.SIGALRM, _alarm_handler)
    signal.alarm(timeout)
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


def api_get(token, path, retries=5):
    url = BASE + path
    for attempt in range(retries):
        req = urllib.request.Request(url, headers={"Authorization": f"Token {token}"})
        try:
            with urlopen_hard(req, timeout=25) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:500]
            if e.code in (429, 500, 502, 503) and attempt < retries - 1:
                time.sleep(5 * (attempt + 1))
                continue
            raise RuntimeError(f"GET {path} -> HTTP {e.code}: {body}")
        except (_HardTimeout, TimeoutError, urllib.error.URLError) as e:
            if attempt < retries - 1:
                time.sleep(5 * (attempt + 1))
                continue
            raise RuntimeError(f"GET {path} -> network error: {e}")
    raise RuntimeError("unreachable")


def api_post(token, path, payload, retries=5):
    url = BASE + path
    data = json.dumps(payload).encode("utf-8")
    for attempt in range(retries):
        req = urllib.request.Request(
            url, data=data,
            headers={"Authorization": f"Token {token}",
                     "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen_hard(req, timeout=25) as r:
                return r.status, r.read().decode("utf-8", "replace")[:1000]
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:1000]
            if e.code in (429, 500, 502, 503) and attempt < retries - 1:
                time.sleep(5 * (attempt + 1))
                continue
            return e.code, body
        except (_HardTimeout, TimeoutError, urllib.error.URLError) as e:
            if attempt < retries - 1:
                time.sleep(5 * (attempt + 1))
                continue
            return 0, f"network error: {e}"
    return 0, "unreachable"

# ---------------------------------------------------------------- CDF helpers
# Stdlib port of the spec's nominal_location_to_cdf_location /
# generate_continuous_cdf / standardize_cdf (openapi.yml).

def _norm_ppf(p):
    """Inverse standard normal CDF (Acklam's approximation)."""
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285984469534e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
               ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if p <= phigh:
        q = p - 0.5
        r = q * q
        return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / \
               (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)
    q = math.sqrt(-2 * math.log(1 - p))
    return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
             ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)


def _norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def nominal_to_location(nominal, q):
    """Map a nominal value to a [0,1] CDF location, per the spec."""
    scaling = q["scaling"]
    rmin, rmax = scaling["range_min"], scaling["range_max"]
    zero = scaling.get("zero_point")
    if zero is not None:
        deriv = (rmax - zero) / (rmin - zero)
        return (math.log((nominal - rmin) * (deriv - 1) + (rmax - rmin))
                - math.log(rmax - rmin)) / math.log(deriv)
    return (nominal - rmin) / (rmax - rmin)


def location_to_nominal(loc, q):
    scaling = q["scaling"]
    rmin, rmax = scaling["range_min"], scaling["range_max"]
    zero = scaling.get("zero_point")
    if zero is not None:
        deriv = (rmax - zero) / (rmin - zero)
        return rmin + ((rmax - rmin) * (deriv ** loc) - (rmax - rmin)) / (deriv - 1)
    return rmin + loc * (rmax - rmin)


def generate_cdf(percentiles, q, below=None, above=None):
    """percentiles: {0-100 float: nominal value}. Returns 201-pt CDF."""
    pts = []
    if below is not None:
        pts.append((0.0, below))
    if above is not None:
        pts.append((1.0, 1 - above))
    for pct, nominal in percentiles.items():
        pts.append((nominal_to_location(nominal, q), pct / 100.0))
    pts.sort()
    if pts[0][0] > 0.0 or pts[-1][0] < 1.0:
        raise ValueError("percentiles must encompass question bounds")

    def at(loc):
        prev = pts[0]
        for cur in pts[1:]:
            if prev[0] <= loc <= cur[0]:
                if cur[0] == prev[0]:
                    return cur[1]
                return prev[1] + (cur[1] - prev[1]) * (loc - prev[0]) / (cur[0] - prev[0])
            prev = cur
        return pts[-1][1]

    n = q.get("inbound_outcome_count") or 200
    return [at(i / n) for i in range(n + 1)]


def standardize_cdf(cdf, q):
    """Port of the spec's standardize_cdf (numpy-free)."""
    lower_open = q["open_lower_bound"]
    upper_open = q["open_upper_bound"]
    n_in = q.get("inbound_outcome_count") or 200
    n = len(cdf)
    cdf = list(cdf)
    scale_lo = 0.0 if lower_open else cdf[0]
    scale_hi = 1.0 if upper_open else cdf[-1]
    mass = scale_hi - scale_lo
    if mass <= 0:
        mass = 1.0
    out = []
    for i, f in enumerate(cdf):
        loc = i / (n - 1)
        rf = (f - scale_lo) / mass
        if lower_open and upper_open:
            out.append(0.988 * rf + 0.01 * loc + 0.001)
        elif lower_open:
            out.append(0.989 * rf + 0.01 * loc + 0.001)
        elif upper_open:
            out.append(0.989 * rf + 0.01 * loc)
        else:
            out.append(0.99 * rf + 0.01 * loc)
    # cap PMF spikes: work in PMF space, cap each step, renormalise
    cap = 0.2 * (200 / n_in)
    pmf = [out[0]] + [out[i] - out[i - 1] for i in range(1, n)] + [1 - out[-1]]
    pmf = [min(p, cap) for p in pmf]
    total = sum(pmf)
    pmf = [p / total for p in pmf]
    cdf2 = []
    s = 0.0
    for p in pmf[:-1]:
        s += p
        cdf2.append(s)
    # enforce bounds exactly
    if not lower_open:
        cdf2[0] = 0.0
    if not upper_open:
        cdf2[-1] = 1.0
    # enforce min step 0.01 / n_in by nudging forward
    min_step = 0.01 / n_in
    for i in range(1, len(cdf2)):
        if cdf2[i] - cdf2[i - 1] < min_step:
            cdf2[i] = cdf2[i - 1] + min_step
    if not upper_open:
        cdf2[-1] = 1.0
    return [round(v, 6) for v in cdf2]


def validate_cdf(cdf, q):
    """Return a list of rule violations (empty = valid)."""
    errs = []
    n_in = q.get("inbound_outcome_count") or 200
    expected = n_in + 1
    if len(cdf) != expected:
        errs.append(f"length {len(cdf)} != {expected}")
        return errs
    min_step = 0.01 / n_in
    max_step = 0.2 * (200 / n_in)
    for i in range(1, len(cdf)):
        d = cdf[i] - cdf[i - 1]
        if d < min_step - 1e-9:
            errs.append(f"step {i} too small: {d:.6f} < {min_step:.6f}")
            break
        if d > max_step + 1e-9:
            errs.append(f"step {i} too big: {d:.6f} > {max_step:.6f}")
            break
    if not q["open_lower_bound"] and cdf[0] != 0.0:
        errs.append(f"closed lower bound but cdf[0]={cdf[0]}")
    if q["open_lower_bound"] and cdf[0] < 0.001 - 1e-9:
        errs.append(f"open lower bound but cdf[0]={cdf[0]} < 0.001")
    if not q["open_upper_bound"] and cdf[-1] != 1.0:
        errs.append(f"closed upper bound but cdf[-1]={cdf[-1]}")
    if q["open_upper_bound"] and cdf[-1] > 0.999 + 1e-9:
        errs.append(f"open upper bound but cdf[-1]={cdf[-1]} > 0.999")
    return errs

# ---------------------------------------------------------------- forecast builders

def cp_latest(q):
    """Community Prediction latest value, or None (usual for bot accounts)."""
    aggs = q.get("aggregations") or {}
    for method in ("recency_weighted", "unweighted", "metaculus_prediction"):
        m = aggs.get(method) or {}
        latest = m.get("latest")
        if latest is not None:
            return latest
    return None


def forecast_binary(q, ev_p=None):
    cp = cp_latest(q)
    if cp is not None:
        # CP for binary: {'probability_yes': x} (or a bare number in some shapes)
        p = cp.get("probability_yes") if isinstance(cp, dict) else cp
        if p is not None:
            p2 = 0.5 + 0.85 * (float(p) - 0.5)
            return {"probability_yes": round(min(0.98, max(0.02, p2)), 4)}, "cp"
    if ev_p is not None:
        return {"probability_yes": round(ev_p, 4)}, "research"
    return {"probability_yes": 0.5}, "fallback-0.5"


def forecast_mc(q, ev_probs=None):
    options = q.get("options") or []
    cp = cp_latest(q)
    if cp is not None and isinstance(cp, dict):
        vals = cp.get("probability_yes_per_category") or cp
        if isinstance(vals, dict) and set(vals) <= set(options) and vals:
            n = len(options)
            out = {}
            for o in options:
                v = float(vals.get(o, 0.0))
                out[o] = 0.85 * v + 0.15 * (1.0 / n)
            s = sum(out.values())
            out = {k: round(v / s, 4) for k, v in out.items()}
            return {"probability_yes_per_category": out}, "cp"
    if ev_probs is not None:
        return {"probability_yes_per_category": ev_probs}, "research"
    n = len(options)
    return {"probability_yes_per_category": {o: round(1.0 / n, 4) for o in options}}, "fallback-uniform"


def cdf_from_percentiles(pcts, q):
    """generate_cdf + standardize_cdf + validate_cdf, with 0.1/99.9 anchor
    percentiles added so the percentile set always encompasses the
    question bounds. Needed when the sd is small or the centre is near a
    bound (e.g. evidence centre 72 on a 0-1000 range with sd 25%: the
    95th percentile alone would not reach the top of the range and
    generate_cdf would raise)."""
    pcts = dict(pcts)
    rmin, rmax = q["scaling"]["range_min"], q["scaling"]["range_max"]
    vals = sorted(pcts.values())
    pcts[0.1] = min(vals[0], rmin)
    pcts[99.9] = max(vals[-1], rmax)
    cdf = generate_cdf(pcts, q)
    cdf = standardize_cdf(cdf, q)
    return cdf, validate_cdf(cdf, q)


def _normal_cdf_forecast(q, center_nominal, sd_frac):
    """Broad normal CDF centred on center_nominal, sd as fraction of range."""
    rmin, rmax = q["scaling"]["range_min"], q["scaling"]["range_max"]
    span = rmax - rmin
    if q["scaling"].get("zero_point") is not None:
        # log-scaled: work in log space for centre/sd
        lo, hi = math.log(max(rmin, 1e-9)), math.log(max(rmax, 1e-9))
        mu = math.log(max(center_nominal, 1e-9))
        sd = sd_frac * (hi - lo)
        pcts = {}
        for pct in (5, 25, 50, 75, 95):
            z = _norm_ppf(pct / 100.0)
            pcts[float(pct)] = math.exp(mu + z * sd)
    else:
        mu = center_nominal
        sd = sd_frac * span
        pcts = {float(p): mu + _norm_ppf(p / 100.0) * sd for p in (5, 25, 50, 75, 95)}
    cdf, errs = cdf_from_percentiles(pcts, q)
    if errs:
        raise ValueError(f"CDF invalid: {errs}")
    return {"continuous_cdf": cdf}


def forecast_continuous(q, ev_center=None, ev_sd=None):
    rmin, rmax = q["scaling"]["range_min"], q["scaling"]["range_max"]
    zero = q["scaling"].get("zero_point")
    if zero is not None:
        center = math.sqrt(max(rmin, 1e-9) * max(rmax, 1e-9))
    else:
        center = (rmin + rmax) / 2.0
    cp = cp_latest(q)
    # CP for continuous questions, when present, carries forecast_values
    # (list of [x, cdf] pairs) — use its median as the centre.
    if cp is not None and isinstance(cp, dict):
        fv = cp.get("forecast_values")
        if fv:
            try:
                xs = [float(a) for a, _ in fv]
                center = sorted(xs)[len(xs) // 2]
                return _normal_cdf_forecast(q, center, 0.25), "cp"
            except Exception:
                pass
    if ev_center is not None:
        return _normal_cdf_forecast(q, ev_center, ev_sd or 0.30), "research"
    return _normal_cdf_forecast(q, center, 0.40), "fallback-midpoint-wide"


def build_forecast(q, item=None, keys=None, use_research=True):
    """Forecast one question. Evidence order: CP -> LLM (if configured)
    -> news research (heuristic) -> honest fallback. `item` carries the
    post title/description for research; without it, research is skipped.
    Returns (payload, source, research_info_dict)."""
    keys = keys or {}
    title = (item or {}).get("post_title", "")
    desc = (item or {}).get("post_description", "")
    articles = []
    research_info = {"source": "none", "n_articles": 0, "query": "",
                     "detail": ""}
    t = q["type"]
    if t not in ("binary", "multiple_choice", "numeric", "discrete", "date"):
        return None, f"unsupported-type-{t}", research_info

    # CP anchor is checked inside the builders; research/LLM only matter
    # when CP is null, so gather evidence first (cheap) and let the
    # builders decide. LLM runs before the heuristic builders because it
    # uses the same news context with strictly validated output.
    ev = None
    if use_research and item is not None:
        try:
            ev = research.research_question(title, desc, keys)
            articles = ev["articles"]
            research_info.update(source=ev["source"],
                                 n_articles=len(articles),
                                 query=ev["query"])
        except Exception as e:
            research_info["detail"] = f"research error: {type(e).__name__}"
            articles, ev = [], None

    if keys.get("openrouter_api_key") and keys.get("openrouter_model"):
        llm_payload, lsrc = research.llm_forecast(
            q, title, desc, articles, keys)
        if llm_payload is not None:
            if "percentiles" in llm_payload:
                cdf, errs = cdf_from_percentiles(llm_payload["percentiles"], q)
                if errs:
                    research_info["detail"] = f"llm CDF invalid: {errs}"
                else:
                    return ({"question": q["id"], "continuous_cdf": cdf},
                            "llm", research_info)
            else:
                return ({"question": q["id"], **llm_payload},
                        "llm", research_info)
        elif lsrc and lsrc != "llm not configured":
            research_info["detail"] = f"llm skipped: {lsrc}"

    if t == "binary":
        p, detail = (research.score_binary_evidence(articles)
                     if articles else (None, "no articles"))
        payload, src = forecast_binary(q, ev_p=p)
    elif t == "multiple_choice":
        probs, detail = (research.score_mc_evidence(articles, q.get("options") or [])
                         if articles else (None, "no articles"))
        payload, src = forecast_mc(q, ev_probs=probs)
    else:
        ev = (research.score_continuous_evidence(articles, q)
              if articles else None)
        if ev is not None and ev[0] is not None:
            center, sd_frac, detail = ev
            payload, src = forecast_continuous(q, ev_center=center, ev_sd=sd_frac)
        else:
            detail = ev[1] if ev else "no articles"
            payload, src = forecast_continuous(q)
    research_info["detail"] = detail
    payload = {"question": q["id"], **payload}
    return payload, src, research_info

# ---------------------------------------------------------------- question listing

def iter_subquestions(post):
    """Yield (question-dict) for a post: single, group members, conditional pair."""
    if post.get("question"):
        yield post["question"]
    for sub in post.get("group_of_questions") or []:
        if sub.get("question"):
            yield sub["question"]
    cond = post.get("conditional") or {}
    for key in ("question_yes", "question_no"):
        sub = cond.get(key) or {}
        if sub.get("question"):
            yield sub["question"]


def already_forecast(q):
    """my_forecasts on the detail endpoint is a dict {'history': [...],
    'latest': {...}|None} — an empty dict is truthy, so check 'latest'."""
    mf = q.get("my_forecasts") or {}
    return bool(mf.get("latest")) or bool(mf.get("history"))


def list_open_questions(token, tournament=TOURNAMENT):
    """Return open questions, each enriched with its post-detail record.

    The list endpoint does NOT include my_forecasts (verified live 23/11/2026:
    the key is absent), so we fetch each post's detail once and use the
    detail's question objects (full scaling/options + my_forecasts) instead
    of the list's partial ones. One detail request per post is cheap:
    a MiniBench round has ~60 posts.
    """
    posts, url = [], (f"/posts/?tournaments={tournament}&statuses=open"
                      f"&with_cp=true&include_descriptions=true&limit=100"
                      f"&order_by=-published_at")
    while url:
        d = api_get(token, url)
        page = d.get("results", [])
        if not page:
            # The API keeps a `next` link populated even past the last
            # page (verified live 23/11/2026: offset ran to 182000 on
            # empty pages). An empty page means we are done.
            break
        posts += page
        if len(posts) >= 5000:  # safety cap; a round has ~60 posts
            break
        nxt = d.get("next")
        url = nxt.replace(BASE, "") if nxt else None
        time.sleep(0.3)
    out = []
    for p in posts:
        try:
            detail = api_get(token, f"/posts/{p['id']}/?with_cp=true"
                                    f"&include_descriptions=true")
        except Exception as e:
            print(f"  WARN: post {p['id']} detail fetch failed: {e}")
            continue
        time.sleep(0.3)
        for q in iter_subquestions(detail):
            if q.get("status") != "open":
                continue
            out.append({"post_id": p["id"], "post_title": p.get("title", ""),
                        "post_description": detail.get("description", "") or "",
                        "question": q})
    return out

# ---------------------------------------------------------------- state

def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"submitted": {}}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=1, sort_keys=True)

# ---------------------------------------------------------------- selftest

def selftest():
    print("== selftest: CDF validation (offline, no network) ==")
    cases = [
        {"id": 1, "type": "numeric",
         "scaling": {"range_min": 0, "range_max": 100, "zero_point": None},
         "open_lower_bound": False, "open_upper_bound": False,
         "inbound_outcome_count": 200},
        {"id": 2, "type": "numeric",
         "scaling": {"range_min": 1, "range_max": 1000, "zero_point": 0},
         "open_lower_bound": True, "open_upper_bound": True,
         "inbound_outcome_count": 200},
        {"id": 3, "type": "discrete",
         "scaling": {"range_min": 0, "range_max": 10, "zero_point": None},
         "open_lower_bound": False, "open_upper_bound": False,
         "inbound_outcome_count": 10},
        {"id": 4, "type": "numeric",
         "scaling": {"range_min": -50, "range_max": 50, "zero_point": None},
         "open_lower_bound": True, "open_upper_bound": False,
         "inbound_outcome_count": 200},
    ]
    ok = True
    for q in cases:
        try:
            payload, src = forecast_continuous(q)
            errs = validate_cdf(payload["continuous_cdf"], q)
            print(f"  case {q['id']} ({q['type']}, "
                  f"open={q['open_lower_bound']}/{q['open_upper_bound']}, "
                  f"inbound={q['inbound_outcome_count']}): len="
                  f"{len(payload['continuous_cdf'])} errs={errs} src={src}")
            ok = ok and not errs
        except Exception as e:
            print(f"  case {q['id']}: EXCEPTION {e}")
            ok = False
    # edge case: centre near a bound + small sd must not crash generate_cdf
    # (research evidence centre 72 on a 0-1000 range, sd 0.25 — the 95th
    # percentile alone does not reach the top of the range)
    q_edge = {"id": 5, "type": "numeric",
              "scaling": {"range_min": 0, "range_max": 1000, "zero_point": None},
              "open_lower_bound": False, "open_upper_bound": False,
              "inbound_outcome_count": 200}
    try:
        p5 = _normal_cdf_forecast(q_edge, 72, 0.25)
        errs5 = validate_cdf(p5["continuous_cdf"], q_edge)
        print(f"  edge case (centre 72 on 0-1000, sd 0.25): errs={errs5}")
        ok = ok and not errs5
    except Exception as e:
        print(f"  edge case: EXCEPTION {e}")
        ok = False
    # binary / MC builders
    b, s = forecast_binary({"id": 9, "type": "binary", "aggregations": None})
    print("  binary fallback:", b, s)
    m, s = forecast_mc({"id": 10, "type": "multiple_choice",
                        "options": ["A", "B", "C"], "aggregations": None})
    print("  mc fallback:", m, s)
    # CP-anchored binary
    b2, s2 = forecast_binary({"id": 11, "type": "binary",
                              "aggregations": {"recency_weighted": {"latest": {"probability_yes": 0.8}}}})
    print("  binary cp-anchored (cp 0.8 -> expect 0.755):", b2, s2)

    print("== selftest: research layer (offline, synthetic articles) ==")
    # keyword extraction
    kws = research.extract_keywords(
        "Will Nvidia's (NVDA) closing share price rise on Friday?",
        "Resolution: Nasdaq official close.")
    print("  keywords:", kws)
    ok = ok and "nvda" in kws and "nvidia" in kws
    # binary cue scoring: 6 unanimous yes-leaning articles -> p in (0.5, 0.8]
    arts_yes = [{"title": f"NVDA surges as analysts confirm record gains {i}",
                 "text": f"NVDA surges as analysts confirm record gains {i}"}
                for i in range(6)]
    p, det = research.score_binary_evidence(arts_yes)
    print(f"  binary 6x yes-leaning -> p={p} ({det})")
    ok = ok and p is not None and 0.5 < p <= 0.80
    # no articles -> None
    p0, _ = research.score_binary_evidence([])
    print("  binary 0 articles ->", p0)
    ok = ok and p0 is None
    # balanced cues -> ~0.5
    arts_mix = [{"title": "X rises after Y falls", "text": "X rises after Y falls"}]
    pm, _ = research.score_binary_evidence(arts_mix)
    print("  binary mixed cues -> p=", pm)
    ok = ok and pm is not None and abs(pm - 0.5) < 0.05
    # MC mention counting
    arts_mc = [
        {"title": "Alpha wins the contract", "text": "Alpha wins the contract"},
        {"title": "Alpha leads the polls", "text": "Alpha leads the polls"},
        {"title": "Beta trails behind", "text": "Beta trails behind"},
    ]
    probs, det = research.score_mc_evidence(arts_mc, ["Alpha", "Beta", "Gamma"])
    print("  mc mentions ->", probs, det)
    ok = ok and probs is not None and probs["Alpha"] > probs["Beta"] > probs["Gamma"]
    # number extraction: years and date parts must not leak in
    nums = research.extract_numbers(
        "On November 27, 2026 the index hit 5,432.10, up 12.5% from 2025.")
    print("  numbers (expect exactly [5432.1, 12.5]):", nums)
    ok = ok and nums == [5432.1, 12.5]
    # date extraction
    dates = research.extract_dates("The vote is scheduled for December 15, 2026.")
    print("  dates (expect 1 timestamp in Dec 2026):", dates)
    ok = ok and len(dates) == 1 and 1796000000 < dates[0] < 1799000000
    # continuous evidence: median of in-range values
    q_num = {"id": 20, "type": "numeric",
             "scaling": {"range_min": 0, "range_max": 1000, "zero_point": None},
             "open_lower_bound": False, "open_upper_bound": False,
             "inbound_outcome_count": 200}
    arts_num = [
        {"title": "Index closes at 812", "text": "Index closes at 812 points"},
        {"title": "Index at 798", "text": "Index at 798 after rally"},
    ]
    ce = research.score_continuous_evidence(arts_num, q_num)
    print("  continuous evidence ->", ce)
    ok = ok and ce is not None and abs(ce[0] - 805.0) < 1.0 and ce[1] == 0.25
    # build_forecast honours research evidence end-to-end (no network:
    # evidence passed via articles is simulated by monkeypatching research)
    _orig = research.research_question
    research.research_question = lambda t, d, k: {
        "query": "test", "articles": arts_yes, "source": "rss"}
    try:
        payload, src, info = build_forecast(
            {"id": 21, "type": "binary", "aggregations": None},
            {"post_title": "Will NVDA rise?", "post_description": ""}, {})
        print("  build_forecast binary w/ research ->", payload, src, info["source"])
        ok = ok and src == "research" and payload["probability_yes"] > 0.5
    finally:
        research.research_question = _orig
    print("SELFTEST", "PASS" if ok else "FAIL")
    return 0 if ok else 1

# ---------------------------------------------------------------- main

def research_test():
    """Live smoke test of the research pipeline on 3 sample questions.
    Prints query, source, article count, evidence and the forecast.
    Never submits anything."""
    keys = load_optional_keys()
    print("== research-test: live news fetch (no submit) ==")
    print(f"  keys configured: {sorted(keys)} (values never printed)")
    samples = [
        {"id": 9001, "type": "binary", "aggregations": None,
         "title": "Will Nvidia's (NVDA) share price close higher next Friday?",
         "description": "Resolution: Nasdaq official closing price."},
        {"id": 9002, "type": "multiple_choice",
         "options": ["Nvidia", "AMD", "Intel"], "aggregations": None,
         "title": "Which chipmaker's stock will gain the most next week?",
         "description": "Resolution: largest percentage gain, Friday close."},
        {"id": 9003, "type": "numeric",
         "scaling": {"range_min": 0, "range_max": 100, "zero_point": None},
         "open_lower_bound": False, "open_upper_bound": False,
         "inbound_outcome_count": 200, "aggregations": None,
         "title": "What will the Crypto Fear & Greed Index record next Friday?",
         "description": "Resolution: alternative.me index value."},
    ]
    ok = True
    for q in samples:
        item = {"post_title": q["title"], "post_description": q["description"]}
        payload, src, info = build_forecast(q, item, keys, use_research=True)
        print(f"  q{q['id']} [{q['type']}] query={info['query']!r} "
              f"source={info['source']} n_articles={info['n_articles']} "
              f"detail={info['detail']}")
        print(f"    -> src={src} payload={json.dumps(payload)[:220]}")
        ok = ok and payload is not None
    print("RESEARCH-TEST", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description="Metaculus MiniBench forecasting bot")
    ap.add_argument("--dry-run", action="store_true", help="print, submit nothing")
    ap.add_argument("--submit", action="store_true", help="submit for real")
    ap.add_argument("--list-only", action="store_true", help="only list open questions")
    ap.add_argument("--selftest", action="store_true", help="offline CDF + research checks")
    ap.add_argument("--research-test", action="store_true",
                    help="live news fetch on 3 sample questions, never submits")
    ap.add_argument("--no-research", action="store_true",
                    help="disable the news-research step (CP/fallback only)")
    ap.add_argument("--fixture", default=None, metavar="FILE",
                    help="offline dry-run against a saved /api/posts/ JSON "
                         "file (real question shapes, no network, never submits)")
    ap.add_argument("--tournament", default=TOURNAMENT)
    ap.add_argument("--max-questions", type=int, default=0,
                    help="cap on questions to forecast (0 = no cap)")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    if args.research_test:
        return research_test()

    keys = load_optional_keys()
    print(f"research keys configured: {sorted(keys)} (values never printed)")

    if args.fixture:
        # Offline verification of the forecast builders against REAL
        # question shapes saved from the API. Fixture questions are closed,
        # so this never submits anything (dry-run is forced). With research
        # ON, the pipeline still fetches live news for each question — that
        # only exercises the mechanics (the news is post-resolution for
        # these closed questions, so the forecasts are NOT meaningful).
        with open(args.fixture) as f:
            data = json.load(f)
        questions = []
        for p in data.get("results", []):
            for q in iter_subquestions(p):
                questions.append({"post_id": p["id"],
                                  "post_title": p.get("title", ""),
                                  "post_description": p.get("description", "") or "",
                                  "question": q})
        print(f"[fixture {args.fixture}] questions: {len(questions)}")
        args.dry_run = True
    else:
        token = load_token()
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S %Z')}] fetching open questions "
              f"(tournament={args.tournament}) ...")
        questions = list_open_questions(token, args.tournament)
        print(f"open questions: {len(questions)}")
    for item in questions:
        q = item["question"]
        print(f"  post {item['post_id']} q{q['id']} [{q['type']}] "
              f"{(item['post_title'] or '')[:70]} | close "
              f"{q.get('scheduled_close_time')} | already forecast: "
              f"{already_forecast(q)}")
    if args.list_only:
        return 0

    if not args.dry_run and not args.submit:
        print("NOTE: no --dry-run / --submit given; defaulting to --dry-run.")
        args.dry_run = True

    state = load_state()
    submitted = state.setdefault("submitted", {})
    n_new = n_skip = n_done = n_fail = 0
    for item in questions:
        q = item["question"]
        qid = str(q["id"])
        if already_forecast(q):
            print(f"  SKIP q{qid}: already forecast on Metaculus")
            n_skip += 1
            continue
        if qid in submitted:
            print(f"  SKIP q{qid}: already submitted by this bot "
                  f"({submitted[qid].get('at')})")
            n_skip += 1
            continue
        payload, src, info = build_forecast(
            q, item, keys, use_research=not args.no_research)
        if payload is None:
            print(f"  SKIP q{qid}: {src}")
            n_skip += 1
            continue
        if args.max_questions and n_new >= args.max_questions:
            print(f"  STOP: --max-questions {args.max_questions} reached")
            break
        n_new += 1
        summary = {k: (v if not isinstance(v, list) else f"<cdf {len(v)} pts>")
                   for k, v in payload.items()}
        print(f"  q{qid} [{q['type']}] src={src} "
              f"news={info['source']}/{info['n_articles']} "
              f"({info['detail']}) payload={json.dumps(summary)[:260]}")
        if args.dry_run:
            continue
        status, body = api_post(token, "/questions/forecast/", [payload])
        print(f"    -> POST /questions/forecast/ HTTP {status}: {body[:300]}")
        if status in (200, 201):
            submitted[qid] = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                              "type": q["type"], "source": src,
                              "post_id": item["post_id"]}
            save_state(state)
            n_done += 1
        else:
            n_fail += 1
        time.sleep(0.5)
    print(f"DONE: considered={len(questions)} new={n_new} skipped={n_skip} "
          f"submitted={n_done} failed={n_fail} mode="
          f"{'DRY-RUN' if args.dry_run else 'SUBMIT'}")
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
