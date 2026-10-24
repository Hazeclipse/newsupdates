"""Builds stories.json for The Digest.

Setup:  pip install requests anthropic
Keys:   export NEWSAPI_KEY=...   export ANTHROPIC_API_KEY=...
Run:    python fetch_news.py     (schedule with cron / GitHub Actions every 1-2 hours)
Host stories.json next to newsdigest.html (Netlify, Vercel, GitHub Pages, etc.).
Note: NewsAPI's free plan is for development only; check its terms for a public site.
"""
import os, json, datetime, difflib, requests, anthropic

NEWSAPI = "https://newsapi.org/v2/everything"
QUERIES = {
    "tech": "technology OR AI OR cybersecurity OR semiconductor",
    "politics": "Congress OR White House OR election OR Supreme Court",
    "crime": "murder OR shooting OR indictment OR arrested OR sentenced OR trial",
}
# Perspective ratings (AllSides-style). Edit freely; unlisted outlets show "Not rated".
BIAS = {
    "Reuters": "Center", "Associated Press": "Center", "AP": "Center", "BBC News": "Center",
    "CNBC": "Center", "ABC News": "Lean Left", "CBS News": "Lean Left", "Politico": "Lean Left",
    "The New York Times": "Lean Left", "The Washington Post": "Lean Left", "The Intercept": "Left",
    "Fox News": "Right", "New York Post": "Lean Right", "Washington Examiner": "Lean Right",
    "National Review": "Lean Right", "Newsmax": "Right",
}

def fetch(query):
    since = (datetime.date.today() - datetime.timedelta(days=2)).isoformat()
    r = requests.get(NEWSAPI, params={
        "q": query, "from": since, "language": "en", "sortBy": "popularity",
        "pageSize": 60, "apiKey": os.environ["NEWSAPI_KEY"]}, timeout=30)
    r.raise_for_status()
    return r.json().get("articles", [])

def cluster(articles):
    """Group articles whose titles are similar (same story, different outlets)."""
    groups = []
    for a in articles:
        t = (a.get("title") or "").lower()
        for g in groups:
            if difflib.SequenceMatcher(None, t, g[0]["title"].lower()).ratio() > 0.55:
                g.append(a); break
        else:
            groups.append([a])
    # Prefer stories covered by several different outlets.
    groups.sort(key=lambda g: len({x["source"]["name"] for x in g}), reverse=True)
    return groups

def summarize(client, group):
    text = "\n".join(f"- {a['source']['name']}: {a['title']}. {a.get('description') or ''}" for a in group[:6])
    msg = client.messages.create(
        model="claude-sonnet-5-5", max_tokens=400,
        messages=[{"role": "user", "content":
            "Using ONLY these headlines/descriptions from different outlets, return JSON with keys "
            "title (neutral headline), sum (1-2 neutral sentences), more (3-4 sentences noting where "
            "outlets differ in framing). No preamble, JSON only.\n" + text}])
    return json.loads(msg.content[0].text)

def main():
    client = anthropic.Anthropic()
    stories = []
    for cat, q in QUERIES.items():
        for g in cluster(fetch(q))[:5]:
            try:
                s = summarize(client, g)
            except Exception:
                continue
            seen, cov = set(), []
            for a in g:
                name = a["source"]["name"]
                if name in seen:
                    continue
                seen.add(name)
                cov.append({"outlet": name, "title": a["title"], "url": a["url"],
                            "bias": BIAS.get(name)})
            stories.append({"cat": cat, "date": g[0]["publishedAt"][:10], **s, "coverage": cov})
    with open("stories.json", "w") as f:
        json.dump({"updated": datetime.datetime.now().strftime("%b %d, %H:%M"), "stories": stories}, f, indent=1)

if __name__ == "__main__":
    main()
