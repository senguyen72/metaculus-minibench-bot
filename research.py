#!/usr/bin/env python3
"""Research + calibration layer for the Metaculus MiniBench bot (v2).

Stdlib only. No paid services, no API keys required for the default path.

Sources (in priority order):
  1. AskNews — ONLY if an ASKNEWS_API_KEY is configured (Metaculus
     partnership: free for registered bot makers, 1k calls/month, 4k total
     per tournament; see PROGRESS.md for how Se obtains the key).
     Endpoint: GET https://api.asknews.app/v1/news/search
     (hours_back=48 -> 1 credit/call). Auth: Bearer token (to be verified
     on first use with a real key; any 401/403 falls back to source 2).
  2. Google News RSS — free, no key, verified reachable from this VM
     27/11/2026. https://news.google.com/rss/search?q=...
  3. GDELT Doc API (ArtList) — free, no key, titles only; used only when
     RSS returns < 3 articles. Rate limit: 1 request / 5s (enforced here).

Evidence -> forecast (heuristic, deliberately humble — see module docstring
in bot.py for the calibration philosophy):
  * binary: directional cue words in article titles/summaries vote yes/no.
    p = 0.5 + sign * 0.30 * agreement * min(1, n_articles/6)
    * min(1, total_cues/4), clamped to [0.20, 0.80]. Zero usable articles
    -> no evidence (fallback 0.5).
  * multiple_choice: mention counts per option (Laplace-smoothed, then
    shrunk 30% toward uniform). Zero mentions -> uniform.
  * numeric/discrete/date: numbers (or dates) extracted from article text,
    filtered to the question's range; median becomes the CDF centre.
    >= 2 values -> sd 25% of range; 1 value -> sd 32%; 0 values -> the
    old fallback (midpoint, sd 40%).

Optional LLM step (dormant until keys exist): if OPENROUTER_API_KEY and
OPENROUTER_MODEL are configured (donated Metaculus credits via OpenRouter),
the question + news summaries are sent to the model and its JSON forecast
is used after strict validation. Any failure -> heuristic evidence.
"""

import json
import math
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

NEWS_TIMEOUT = 15
RSS_URL = "https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
GDELT_URL = ("https://api.gdeltproject.org/api/v2/doc/doc?query={q}"
             "&mode=ArtList&maxrecords=10&format=json")
ASKNEWS_URL = "https://api.asknews.app/v1/news/search"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
UA = {"User-Agent": "Mozilla/5.0 (compatible; MetaculusBot/2.0; +https://github.com/senguyen72/metaculus-minibench-bot)"}

STOPWORDS = set("""
a an the and or of to in on for with by at from as is are was were will would
can could should may might be been has have had do does did not no yes if
when what which who whom how many much more most other some such than then
there here this that these those its it he she they we you i me my his her
their our your about into over under after before between through during
against within without per via upon among towards toward until while
resolution resolve resolved resolves
january february march april may june july august september october
november december monday tuesday wednesday thursday friday saturday sunday
today tomorrow yesterday tonight
announce announces announced report reports reported say says said
next last first week month year day days
del de la el los las der die das le les des
""".split())

# Directional cue words (whole-word match, counted once per article).
YES_CUES = [
    "announce", "announces", "announced", "confirm", "confirms", "confirmed",
    "approve", "approves", "approved", "pass", "passes", "passed",
    "win", "wins", "won", "rise", "rises", "rose", "risen",
    "surge", "surges", "surged", "increase", "increases", "increased",
    "gain", "gains", "gained", "reach", "reaches", "reached",
    "launch", "launches", "launched", "sign", "signs", "signed",
    "agree", "agrees", "agreed", "exceed", "exceeds", "exceeded",
    "beat", "beats", "beaten", "hit", "hits", "record", "records",
    "unveil", "unveils", "unveiled", "soar", "soars", "soared",
    "climb", "climbs", "climbed", "rally", "rallies", "rallied",
    "secure", "secures", "secured", "award", "awards", "awarded",
]
NO_CUES = [
    "reject", "rejects", "rejected", "deny", "denies", "denied",
    "fail", "fails", "failed", "failure", "delay", "delays", "delayed",
    "drop", "drops", "dropped", "fall", "falls", "fell", "fallen",
    "decline", "declines", "declined", "lose", "loses", "lost",
    "block", "blocks", "blocked", "miss", "misses", "missed",
    "cancel", "cancels", "cancelled", "postpone", "postpones", "postponed",
    "unlikely", "plunge", "plunges", "plunged", "sink", "sinks", "sank",
    "cut", "cuts", "suspend", "suspends", "suspended",
    "withdraw", "withdraws", "withdrew", "withdrawn",
    "halt", "halts", "halted", "scrap", "scraps", "scrapped",
    "slump", "slumps", "slumped", "tumble", "tumbles", "tumbled",
]

MONTHS = {m: i + 1 for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"])}
MONTH_ABBR = {m[:3]: i + 1 for m, i in MONTHS.items()}

# ---------------------------------------------------------------- HTTP helpers

def _http_get(url, headers=None, timeout=NEWS_TIMEOUT):
    req = urllib.request.Request(url, headers=headers or UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _http_post_json(url, payload, headers=None, timeout=30):
    data = json.dumps(payload).encode("utf-8")
    h = {"Content-Type": "application/json"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)

# ---------------------------------------------------------------- query building

def extract_keywords(title, description=""):
    """Top keywords for a news query: capitalised entities, tickers,
    numbers-with-units and distinctive lowercase words (stopwords removed).
    Title words score double — descriptions are mostly boilerplate
    ('Resolution: ...') and must not drown out the question itself."""
    text = f"{title or ''} {description or ''}"
    # Tickers like (NVDA), (MSTR)
    tickers = re.findall(r"\(([A-Z]{1,5})\)", text)
    scored = {}
    for chunk, wgt in ((title or "", 2), (description or "", 1)):
        for w in re.findall(r"[A-Za-z][A-Za-z\-']+", chunk):
            lw = w.lower()
            lw = re.sub(r"'s$", "", lw)  # "nvidia's" -> "nvidia"
            if lw in STOPWORDS or len(lw) < 3:
                continue
            # capitalised words and long distinctive words score higher
            s = wgt * (2 if w[0].isupper() else 1)
            s += 1 if len(lw) >= 6 else 0
            scored[lw] = scored.get(lw, 0) + s
    ranked = sorted(scored.items(), key=lambda kv: (-kv[1], kv[0]))
    kws = []
    for t in tickers:
        kws.append(t.lower())
    for w, _ in ranked:
        if w not in kws:
            kws.append(w)
        if len(kws) >= 6:
            break
    return kws


def build_query(title, description=""):
    return " ".join(extract_keywords(title, description))

# ---------------------------------------------------------------- news fetching

def fetch_rss_news(query, limit=10):
    """Google News RSS. Returns [{title, text}] (text = title + description).
    One retry: the VM's egress stalls intermittently, and a single failed
    fetch must not zero out the evidence for a whole question."""
    url = RSS_URL.format(q=urllib.parse.quote(query))
    for attempt in range(2):
        try:
            raw = _http_get(url)
            root = ET.fromstring(raw)
            out = []
            for item in root.iter("item"):
                t = (item.findtext("title") or "").strip()
                d = (item.findtext("description") or "").strip()
                d = re.sub(r"<[^>]+>", " ", d)
                if t:
                    out.append({"title": t, "text": f"{t} {d}".strip()})
                if len(out) >= limit:
                    break
            return out
        except Exception as e:
            print(f"  WARN: RSS fetch failed (attempt {attempt + 1}): "
                  f"{type(e).__name__}", file=sys.stderr)
            time.sleep(2)
    return []


def fetch_gdelt_news(query, limit=10):
    """GDELT Doc API, ArtList mode (titles only). 5s spacing enforced."""
    time.sleep(5.0)
    url = GDELT_URL.format(q=urllib.parse.quote(query))
    try:
        raw = _http_get(url)
        data = json.loads(raw.decode("utf-8", "replace"))
        out = []
        for a in data.get("articles", [])[:limit]:
            t = (a.get("title") or "").strip()
            if t:
                out.append({"title": t, "text": t})
        return out
    except Exception:
        return []


def fetch_asknews(query, api_key, limit=10):
    """AskNews /v1/news/search (hours_back=48 -> 1 credit/call).
    Auth: Bearer token. Returns [{title, text}] or [] on any failure
    (bad key, 403 scope, parse error) — caller falls back to RSS."""
    if not api_key:
        return []
    params = {
        "query": query,
        "hours_back": 48,
        "n_articles": min(limit, 10),
        "return_type": "dicts",
        "method": "kw",
    }
    url = ASKNEWS_URL + "?" + urllib.parse.urlencode(params)
    try:
        raw = _http_get(url, headers=dict(
            UA, Authorization=f"Bearer {api_key}"), timeout=NEWS_TIMEOUT)
        data = json.loads(raw.decode("utf-8", "replace"))
        items = data.get("as_dicts") or data.get("articles") or []
        out = []
        for a in items[:limit]:
            t = (a.get("article_title") or a.get("title") or "").strip()
            s = (a.get("article_summary") or a.get("summary") or "").strip()
            if t:
                out.append({"title": t, "text": f"{t} {s}".strip()})
        return out
    except Exception:
        return []


def research_question(title, description, keys):
    """Fetch news evidence for one question.

    keys: dict with optional 'asknews_api_key'. Returns
    {'query': str, 'articles': [...], 'source': 'asknews'|'rss'|'gdelt'|'none'}.
    Never raises; empty articles means 'no evidence' (caller falls back).
    """
    query = build_query(title, description)
    articles, source = [], "none"
    if keys.get("asknews_api_key"):
        articles = fetch_asknews(query, keys["asknews_api_key"])
        source = "asknews" if articles else "none"
    if not articles:
        articles = fetch_rss_news(query)
        source = "rss" if articles else "none"
    if len(articles) < 3:
        extra = fetch_gdelt_news(query)
        seen = {a["title"] for a in articles}
        for a in extra:
            if a["title"] not in seen:
                articles.append(a)
        if articles and source == "none":
            source = "gdelt"
    return {"query": query, "articles": articles[:10], "source": source}

# ---------------------------------------------------------------- evidence scoring

def _count_cues(text, cues):
    n = 0
    for cue in cues:
        if re.search(r"\b" + re.escape(cue) + r"\b", text):
            n += 1
    return n


def score_binary_evidence(articles):
    """Return (p, detail) or (None, reason) when there is no signal."""
    if not articles:
        return None, "no articles"
    yes = no = 0
    for a in articles:
        t = a["text"].lower()
        yes += _count_cues(t, YES_CUES)
        no += _count_cues(t, NO_CUES)
    total = yes + no
    if total == 0:
        return None, "no directional cues"
    agreement = abs(yes - no) / total
    n = len(articles)
    # Strength needs BOTH agreement and cue volume: a single cue word in
    # 10 articles must not move p to the cap. Full strength needs 4+ cues.
    vol = min(1.0, total / 4.0)
    shift = 0.30 * agreement * min(1.0, n / 6.0) * vol
    p = 0.5 + (shift if yes > no else -shift)
    p = min(0.80, max(0.20, p))
    return round(p, 4), f"yes={yes} no={no} n={n}"


def score_mc_evidence(articles, options):
    """Return (probs dict, detail) or (None, reason)."""
    if not articles or not options:
        return None, "no articles/options"
    counts = {o: 0 for o in options}
    for a in articles:
        t = a["text"].lower()
        for o in options:
            if o.lower() in t:
                counts[o] += 1
    total = sum(counts.values())
    if total == 0:
        return None, "no option mentions"
    n = len(options)
    out = {}
    for o in options:
        smoothed = (counts[o] + 1) / (total + n)
        out[o] = 0.70 * smoothed + 0.30 * (1.0 / n)
    s = sum(out.values())
    out = {k: round(v / s, 4) for k, v in out.items()}
    # keep every option >= 0.02 after renormalisation
    out = {k: max(0.02, v) for k, v in out.items()}
    s = sum(out.values())
    out = {k: round(v / s, 4) for k, v in out.items()}
    return out, f"mentions={counts}"


_DATE_RES = [
    # November 27, 2026 / Nov 27, 2026
    re.compile(r"(?P<m>[A-Za-z]+)\.?\s+(?P<d>\d{1,2}),?\s+(?P<y>\d{4})"),
    # 27 November 2026
    re.compile(r"(?P<d>\d{1,2})\s+(?P<m>[A-Za-z]+)\.?\s+(?P<y>\d{4})"),
    # 2026-11-27 / 2026/11/27
    re.compile(r"(?P<y>\d{4})[-/](?P<mo>\d{1,2})[-/](?P<d>\d{1,2})"),
]


def _month_num(name):
    name = name.lower().rstrip(".")
    return MONTHS.get(name) or MONTH_ABBR.get(name[:3])


def extract_dates(text):
    """Unix timestamps for dates mentioned in text."""
    import calendar
    out = []
    for rx in _DATE_RES:
        for m in rx.finditer(text):
            try:
                g = m.groupdict()
                if "m" in g and g.get("m"):
                    mo = _month_num(g["m"])
                    if not mo:
                        continue
                    y, d = int(g["y"]), int(g["d"])
                else:
                    y, mo, d = int(g["y"]), int(g["mo"]), int(g["d"])
                if 2020 <= y <= 2040 and 1 <= mo <= 12 and 1 <= d <= 31:
                    out.append(calendar.timegm((y, mo, d, 12, 0, 0, 0, 0, 0)))
            except Exception:
                continue
    return out


_NUM_RE = re.compile(
    r"\$?\s?(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s?"
    r"(trillion|billion|million|thousand|percent|%)?",
    re.IGNORECASE)
_MAG = {"thousand": 1e3, "million": 1e6, "billion": 1e9, "trillion": 1e12,
        "percent": 1.0, "%": 1.0}


def extract_numbers(text):
    """Numbers from article text. Date substrings are removed first so
    'November 27, 2026' does not leak 27 / 2026 as quantities; 4-digit
    years (1900-2100) are skipped outright."""
    clean = text
    for rx in _DATE_RES:
        clean = rx.sub(" ", clean)
    out = []
    for m in _NUM_RE.finditer(clean):
        raw, mag = m.group(1), (m.group(2) or "").lower()
        try:
            v = float(raw.replace(",", ""))
        except ValueError:
            continue
        if mag in ("thousand", "million", "billion", "trillion"):
            v *= _MAG[mag]
        # skip bare years
        if v == int(v) and 1900 <= v <= 2100 and not mag:
            continue
        if abs(v) > 1e15:
            continue
        out.append(v)
    return out


def score_continuous_evidence(articles, q):
    """Return (center, sd_frac, detail) or (None, reason)."""
    if not articles:
        return None, "no articles"
    rmin, rmax = q["scaling"]["range_min"], q["scaling"]["range_max"]
    vals = []
    for a in articles:
        if q["type"] == "date":
            vals += [v for v in extract_dates(a["text"])
                     if rmin <= v <= rmax]
        else:
            vals += [v for v in extract_numbers(a["text"])
                     if rmin <= v <= rmax]
    if not vals:
        return None, "no in-range values"
    vals.sort()
    center = vals[len(vals) // 2] if len(vals) % 2 == 1 else \
        (vals[len(vals) // 2 - 1] + vals[len(vals) // 2]) / 2.0
    sd_frac = 0.25 if len(vals) >= 2 else 0.32
    return (center, sd_frac,
            f"n_values={len(vals)} median={center:.4g} range=[{rmin:.4g},{rmax:.4g}]")

# ---------------------------------------------------------------- optional LLM step

def llm_forecast(q, title, description, articles, keys):
    """Ask the configured OpenRouter model for a forecast. Returns
    (payload_dict_without_question, 'llm') or (None, reason). Only runs
    when both OPENROUTER_API_KEY and OPENROUTER_MODEL are configured.
    Output is validated strictly; anything off-spec -> None (fallback)."""
    key = keys.get("openrouter_api_key")
    model = keys.get("openrouter_model")
    if not key or not model:
        return None, "llm not configured"
    news = "\n".join(
        f"- {a['title'][:200]}" for a in articles[:6])
    qtype = q["type"]
    if qtype == "binary":
        schema = ('{"probability_yes": <number 0.02-0.98>}')
        extra = ""
    elif qtype == "multiple_choice":
        schema = ('{"probability_yes_per_category": '
                  '{"<option>": <number>, ...}} (all options, sum to 1)')
        extra = f"Options: {q.get('options')}"
    else:
        schema = ('{"percentiles": {"5": <number>, "25": <number>, '
                  '"50": <number>, "75": <number>, "95": <number>}} '
                  '(strictly increasing, inside the range)')
        extra = (f"Range: {q['scaling']['range_min']} to "
                 f"{q['scaling']['range_max']}"
                 + (" (log scale)" if q["scaling"].get("zero_point") is not None else ""))
    prompt = (
        f"Forecasting question: {title}\n"
        f"Type: {qtype}\n{extra}\n"
        f"Details: {(description or '')[:1200]}\n"
        f"Recent news headlines:\n{news or '(no news found)'}\n\n"
        f"Give your best calibrated forecast as JSON ONLY, exactly this shape: {schema}\n"
        f"Be honest about uncertainty: use values near 0.5 / wide percentile "
        f"spreads when the evidence is weak. No explanation, JSON only.")
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": "You are a careful forecaster. Reply with JSON only."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0,
        "max_tokens": 600,
    }
    try:
        resp = _http_post_json(
            OPENROUTER_URL, body,
            headers={"Authorization": f"Bearer {key}",
                     "HTTP-Referer": "https://github.com/senguyen72/metaculus-minibench-bot",
                     "X-Title": "metaculus-minibench-bot"},
            timeout=40)
        content = resp["choices"][0]["message"]["content"]
    except Exception as e:
        return None, f"llm request failed: {type(e).__name__}"
    try:
        content = content.strip()
        if content.startswith("```"):
            content = re.sub(r"^```(?:json)?\s*", "", content)
            content = re.sub(r"\s*```$", "", content)
        data = json.loads(content)
    except Exception:
        return None, "llm output not JSON"
    try:
        if qtype == "binary":
            p = float(data["probability_yes"])
            if not 0.02 <= p <= 0.98:
                return None, "llm binary out of range"
            return {"probability_yes": round(p, 4)}, "llm"
        if qtype == "multiple_choice":
            vals = data["probability_yes_per_category"]
            opts = q.get("options") or []
            if set(vals) != set(opts):
                return None, "llm MC options mismatch"
            tot = sum(float(v) for v in vals.values())
            if not 0.95 <= tot <= 1.05:
                return None, "llm MC does not sum to 1"
            out = {k: round(max(0.02, float(v)) / tot, 4)
                   for k, v in vals.items()}
            s = sum(out.values())
            out = {k: round(v / s, 4) for k, v in out.items()}
            return {"probability_yes_per_category": out}, "llm"
        pcts = data["percentiles"]
        vals = [float(pcts[str(p)]) for p in (5, 25, 50, 75, 95)]
        if any(vals[i] >= vals[i + 1] for i in range(4)):
            return None, "llm percentiles not increasing"
        rmin, rmax = q["scaling"]["range_min"], q["scaling"]["range_max"]
        if not (rmin <= vals[0] and vals[-1] <= rmax):
            return None, "llm percentiles outside range"
        return {"percentiles": {float(p): v for p, v in
                                zip((5, 25, 50, 75, 95), vals)}}, "llm"
    except (KeyError, TypeError, ValueError) as e:
        return None, f"llm output invalid: {e}"
