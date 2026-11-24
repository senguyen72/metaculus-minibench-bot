#!/usr/bin/env python3
"""
Metaculus MiniBench forecasting bot — baseline v1 (stdlib only, no paid services).

Bot account: senguyen72-bot (first bot on the account — the only one eligible
for MiniBench prizes). Token is read from config.json (same directory) or the
METACULUS_TOKEN environment variable. The token is NEVER printed or logged.

Forecast logic (honest baseline, documented here and in PROGRESS.md):
  * Anchor: the Community Prediction (CP), when the API returns it.
    - Binary:  p' = 0.5 + 0.85 * (p_cp - 0.5), clamped to [0.02, 0.98].
    - Multiple choice: shrink each category toward uniform by 15%,
      then renormalise to sum 1.
    - Numeric/discrete/date: build a normal CDF centred on the CP median
      (or CP mean if no median), sd = max(0.25 * range, CP-implied sd).
  * Fallback when CP is null (the normal case for bot accounts: the official
    API spec says aggregation data is null except on posts in an accessible
    project — MiniBench posts are NOT in one for this account):
    - Binary: 0.5 (maximum entropy — we claim no edge).
    - Multiple choice: uniform over the options.
    - Numeric/discrete/date: broad normal CDF centred on the range midpoint
      (geometric midpoint for log-scaled questions), sd = 0.40 of the range.
    This fallback is deliberately uninformative rather than fake-precise:
    a wrong confident forecast scores far worse than a humble one.
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
  python3 bot.py --selftest                           # offline CDF validation
  python3 bot.py --list-only                          # list open questions
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


def forecast_binary(q):
    cp = cp_latest(q)
    if cp is not None:
        # CP for binary: {'probability_yes': x} (or a bare number in some shapes)
        p = cp.get("probability_yes") if isinstance(cp, dict) else cp
        if p is not None:
            p2 = 0.5 + 0.85 * (float(p) - 0.5)
            return {"probability_yes": round(min(0.98, max(0.02, p2)), 4)}, "cp"
    return {"probability_yes": 0.5}, "fallback-0.5"


def forecast_mc(q):
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
    n = len(options)
    return {"probability_yes_per_category": {o: round(1.0 / n, 4) for o in options}}, "fallback-uniform"


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
    cdf = generate_cdf(pcts, q)
    cdf = standardize_cdf(cdf, q)
    errs = validate_cdf(cdf, q)
    if errs:
        raise ValueError(f"CDF invalid: {errs}")
    return {"continuous_cdf": cdf}


def forecast_continuous(q):
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
    return _normal_cdf_forecast(q, center, 0.40), "fallback-midpoint-wide"


def build_forecast(q):
    t = q["type"]
    if t == "binary":
        payload, src = forecast_binary(q)
    elif t == "multiple_choice":
        payload, src = forecast_mc(q)
    elif t in ("numeric", "discrete", "date"):
        payload, src = forecast_continuous(q)
    else:
        return None, f"unsupported-type-{t}"
    payload = {"question": q["id"], **payload}
    return payload, src

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
    print("SELFTEST", "PASS" if ok else "FAIL")
    return 0 if ok else 1

# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="Metaculus MiniBench baseline bot")
    ap.add_argument("--dry-run", action="store_true", help="print, submit nothing")
    ap.add_argument("--submit", action="store_true", help="submit for real")
    ap.add_argument("--list-only", action="store_true", help="only list open questions")
    ap.add_argument("--selftest", action="store_true", help="offline CDF checks")
    ap.add_argument("--fixture", default=None, metavar="FILE",
                    help="offline dry-run against a saved /api/posts/ JSON "
                         "file (real question shapes, no network, never submits)")
    ap.add_argument("--tournament", default=TOURNAMENT)
    ap.add_argument("--max-questions", type=int, default=0,
                    help="cap on questions to forecast (0 = no cap)")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    if args.fixture:
        # Offline verification of the forecast builders against REAL
        # question shapes saved from the API. Fixture questions are closed,
        # so this never submits anything (dry-run is forced).
        with open(args.fixture) as f:
            data = json.load(f)
        questions = []
        for p in data.get("results", []):
            for q in iter_subquestions(p):
                questions.append({"post_id": p["id"],
                                  "post_title": p.get("title", ""),
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
        payload, src = build_forecast(q)
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
        print(f"  q{qid} [{q['type']}] src={src} payload={json.dumps(summary)[:300]}")
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
