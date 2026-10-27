"""Builds the daily news data for The Digest.

Secrets needed: NEWSAPI_KEY, OPENAI_API_KEY
Output: data/YYYY-MM-DD.json (one per run), data/index.json (list of dates),
        data/bias_cache.json (AI bias guesses, reused so we only pay once per outlet)
Files older than KEEP_DAYS are deleted; everything newer is kept.
"""
import os, re, json, glob, datetime, difflib, requests
from urllib.parse import urlparse
from openai import OpenAI

MODEL = "gpt-4o-mini"      # swap for a newer cheap model if you like
LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "2"))   # how far back this run searches
KEEP_DAYS = 60             # ~2 months of history
# More stories per section when backfilling several days at once
PER_SECTION = int(os.environ.get("PER_SECTION", "5" if LOOKBACK_DAYS <= 2 else "20"))
NEWSAPI = "https://newsapi.org/v2/everything"
DATA = "data"

# ---------- WHAT TO COVER (edit freely) ----------
TECH_Q = "technology OR AI OR cybersecurity OR semiconductor OR software OR startup"
CRIME_Q = "murder OR shooting OR indictment OR arrested OR sentenced OR trial OR kidnapping OR fraud"
# Multilingual politics terms so non-English outlets match too
POL_Q = ("election OR government OR parliament OR minister OR president OR protest OR "
         "élection OR gouvernement OR gobierno OR elecciones OR governo OR eleição OR "
         "Regierung OR Wahl OR 政府 OR 选举 OR الحكومة OR انتخابات")

# Region -> outlet domains (domains also let us rate bias reliably)
REGIONS = {
    "namerica": "nytimes.com,washingtonpost.com,cnn.com,foxnews.com,apnews.com,reuters.com,politico.com,thehill.com,cbc.ca,theglobeandmail.com,eluniversal.com.mx,nbcnews.com,npr.org",
    "samerica": "clarin.com,lanacion.com.ar,infobae.com,folha.uol.com.br,g1.globo.com,eltiempo.com,elcomercio.pe,mercopress.com,latercera.com",
    "europe": "bbc.co.uk,bbc.com,theguardian.com,lemonde.fr,lefigaro.fr,spiegel.de,dw.com,elpais.com,corriere.it,politico.eu,euronews.com,france24.com",
    "eastasia": "scmp.com,japantimes.co.jp,koreaherald.com,koreatimes.co.kr,taipeitimes.com,globaltimes.cn,asia.nikkei.com,chinadaily.com.cn,focustaiwan.tw",
    "southasia": "thehindu.com,hindustantimes.com,timesofindia.indiatimes.com,dawn.com,thedailystar.net,ndtv.com,tribune.com.pk,kathmandupost.com",
    "africa": "allafrica.com,news24.com,theeastafrican.co.ke,nation.africa,premiumtimesng.com,mg.co.za,africanews.com",
    "mideast": "aljazeera.com,timesofisrael.com,haaretz.com,arabnews.com,jpost.com,middleeasteye.net,thenationalnews.com,english.alarabiya.net",
}

# TEMPORARY research topics. Delete or replace entries when you move on.
RESEARCH = [
    {"topic": "French protests", "searches": [("France protest OR strike OR demonstration", "en"),
                                               ("manifestation OR grève OR manifestants France", "fr")]},
    {"topic": "Protests around the world", "searches": [("protesters OR demonstrators OR protest", "en"),
                                                         ("protestas OR manifestantes", "es")]},
    {"topic": "U.S. midterm elections", "searches": [("midterm elections 2026 OR Senate race OR House race", "en")]},
]

# ---------- BIAS (by domain). Best-effort AllSides-style; verify and edit. ----------
BIAS = {
    "cnn.com": "Lean Left", "nytimes.com": "Lean Left", "washingtonpost.com": "Lean Left",
    "theguardian.com": "Lean Left", "nbcnews.com": "Lean Left", "abcnews.go.com": "Lean Left",
    "cbsnews.com": "Lean Left", "politico.com": "Lean Left", "axios.com": "Lean Left",
    "time.com": "Lean Left", "usatoday.com": "Lean Left", "bloomberg.com": "Lean Left",
    "npr.org": "Center", "huffpost.com": "Left", "msnbc.com": "Left", "vox.com": "Left",
    "slate.com": "Left", "theintercept.com": "Left", "motherjones.com": "Left", "jacobin.com": "Left",
    "reuters.com": "Center", "apnews.com": "Center", "bbc.com": "Center", "bbc.co.uk": "Center",
    "thehill.com": "Center", "newsweek.com": "Center", "forbes.com": "Center", "cnbc.com": "Center",
    "wsj.com": "Center", "dw.com": "Center", "france24.com": "Center", "euronews.com": "Center",
    "foxnews.com": "Lean Right", "nypost.com": "Lean Right", "washingtonexaminer.com": "Lean Right",
    "washingtontimes.com": "Lean Right", "theepochtimes.com": "Lean Right",
    "nationalreview.com": "Right", "newsmax.com": "Right", "dailywire.com": "Right",
    "breitbart.com": "Right", "theblaze.com": "Right", "dailycaller.com": "Right", "thefederalist.com": "Right",
    "aljazeera.com": "Lean Left", "politico.eu": "Lean Left",
}
STATE = {"globaltimes.cn", "chinadaily.com.cn", "xinhuanet.com", "cgtn.com", "rt.com",
         "tass.com", "presstv.ir", "sputnikglobe.com"}
LABELS = ["Left", "Lean Left", "Center", "Lean Right", "Right"]


def domain_of(url):
    return urlparse(url).netloc.lower().removeprefix("www.")


def match(domain, table):
    for d in table:
        if domain == d or domain.endswith("." + d):
            return d
    return None


def load_cache():
    try:
        return json.load(open(f"{DATA}/bias_cache.json"))
    except Exception:
        return {}


def bias_of(domain, cache):
    if match(domain, STATE):
        return None, "state"
    k = match(domain, BIAS)
    if k:
        return BIAS[k], "table"
    if cache.get(domain) in LABELS:
        return cache[domain], "ai"
    return None, "none"


def estimate_bias(client, domains, cache):
    todo = [d for d in domains if d not in cache and not match(d, BIAS) and not match(d, STATE)]
    if not todo:
        return
    try:
        r = client.chat.completions.create(
            model=MODEL, response_format={"type": "json_object"},
            messages=[{"role": "user", "content":
                "For each news outlet domain, estimate its general political lean relative to the "
                "mainstream politics of its home country. Use exactly one of: Left, Lean Left, Center, "
                "Lean Right, Right, Unknown. Say Unknown if you are not fairly sure. "
                'Return JSON like {"domain.com": "Center"}.\n' + "\n".join(todo)}])
        res = json.loads(r.choices[0].message.content)
    except Exception as e:
        print("bias estimate failed:", e)
        return
    for d in todo:
        cache[d] = res.get(d, "Unknown")


def get(q, lang=None, domains=None):
    since = (datetime.date.today() - datetime.timedelta(days=LOOKBACK_DAYS)).isoformat()
    p = {"q": q, "from": since, "sortBy": "popularity", "pageSize": 100,
         "apiKey": os.environ["NEWSAPI_KEY"]}
    if lang:
        p["language"] = lang
    if domains:
        p["domains"] = domains
    r = requests.get(NEWSAPI, params=p, timeout=30)
    r.raise_for_status()
    return [a for a in r.json().get("articles", [])
            if a.get("title") and "[Removed]" not in a["title"] and a.get("url")]


def cluster(articles):
    groups = []
    for a in articles:
        t = a["title"].lower()
        for g in groups:
            if difflib.SequenceMatcher(None, t, g[0]["title"].lower()).ratio() > 0.55:
                g.append(a)
                break
        else:
            groups.append([a])
    groups.sort(key=lambda g: len({x["source"]["name"] for x in g}), reverse=True)
    return groups


def summarize(client, group):
    arts = group[:6]
    text = "\n".join(f"{i+1}. [{a['source']['name']}] {a['title']}. {a.get('description') or ''}"
                     for i, a in enumerate(arts))
    r = client.chat.completions.create(
        model=MODEL, response_format={"type": "json_object"},
        messages=[{"role": "user", "content":
            "The articles below may be in different languages (French, Spanish, Arabic, Chinese, etc.). "
            "Write EVERYTHING in English. Using ONLY the text below, return JSON with keys: "
            "title (neutral English headline), sum (1-2 neutral sentences), "
            "more (3-4 sentences; if several outlets, note where framing differs), "
            "headlines_en (array of English translations of each numbered headline, in order). "
            "Do not add facts that are not in the text.\n" + text}])
    d = json.loads(r.choices[0].message.content)
    en = d.get("headlines_en") or []
    return {"title": d["title"], "sum": d["sum"], "more": d.get("more", "")}, en


def build(client, cache, articles, meta):
    out = []
    for g in cluster(articles)[:PER_SECTION]:
        estimate_bias(client, {domain_of(a["url"]) for a in g[:6]}, cache)
        try:
            s, en = summarize(client, g)
        except Exception as e:
            print("summary failed:", e)
            continue
        cov, seen = [], set()
        for i, a in enumerate(g[:6]):
            name = a["source"]["name"]
            if name in seen:
                continue
            seen.add(name)
            b, src = bias_of(domain_of(a["url"]), cache)
            cov.append({"outlet": name, "title": a["title"],
                        "title_en": en[i] if i < len(en) and isinstance(en[i], str) else a["title"],
                        "url": a["url"], "bias": b, "src": src})
        out.append({**meta, "date": g[0]["publishedAt"][:10], **s, "coverage": cov})
    return out


def prune_and_index(today):
    cutoff = today - datetime.timedelta(days=KEEP_DAYS)
    dates = []
    for f in glob.glob(f"{DATA}/*.json"):
        m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})\.json", os.path.basename(f))
        if not m:
            continue
        d = datetime.date.fromisoformat(m.group(1))
        if d < cutoff:
            os.remove(f)
        else:
            dates.append(m.group(1))
    json.dump({"dates": sorted(dates)}, open(f"{DATA}/index.json", "w"))


def main():
    os.makedirs(DATA, exist_ok=True)
    client = OpenAI()
    cache = load_cache()
    stories = []

    def run(meta, searches):
        arts = []
        for q, lang, dom in searches:
            try:
                arts += get(q, lang, dom)
            except Exception as e:
                print("search failed:", meta, e)
        stories.extend(build(client, cache, arts, meta))

    run({"cat": "tech"}, [(TECH_Q, "en", None)])
    run({"cat": "crime"}, [(CRIME_Q, "en", None)])
    for region, doms in REGIONS.items():
        run({"cat": "politics", "region": region}, [(POL_Q, None, doms)])
    for item in RESEARCH:
        run({"cat": "research", "topic": item["topic"]}, [(q, l, None) for q, l in item["searches"]])

    if not stories:
        print("No stories built; nothing written.")
        return
    today = datetime.datetime.now(datetime.timezone.utc).date()
    updated = datetime.datetime.utcnow().strftime("%b %d, %H:%M UTC")
    # Save each story under the day it was published, merging with any existing file for that day
    by_day = {}
    for s in stories:
        by_day.setdefault(s["date"], []).append(s)
    for day, items in by_day.items():
        path = f"{DATA}/{day}.json"
        old = []
        if os.path.exists(path):
            try:
                old = json.load(open(path)).get("stories", [])
            except Exception:
                old = []
        seen = {c["url"] for st in old for c in st["coverage"]}
        fresh = [st for st in items if not any(c["url"] in seen for c in st["coverage"])]
        json.dump({"date": day, "updated": updated, "stories": old + fresh},
                  open(path, "w"), indent=1)
    json.dump(cache, open(f"{DATA}/bias_cache.json", "w"), indent=1)
    prune_and_index(today)
    print(f"Built {len(stories)} stories across {len(by_day)} day(s).")


if __name__ == "__main__":
    main()
