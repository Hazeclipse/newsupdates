"""Builds stories.json for The Digest.

Keys (set as GitHub secrets): NEWSAPI_KEY, OPENAI_API_KEY
Run: python fetch_news.py
"""
import os, json, datetime, difflib, requests
from openai import OpenAI

NEWSAPI = "https://newsapi.org/v2/everything"
MODEL = "gpt-4o-mini"  # swap for a newer cheap model if you like

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
    arts = r.json().get("articles", [])
    # NewsAPI returns deleted articles with "[Removed]" titles
    return [a for a in arts if a.get("title") and "[Removed]" not in a["title"]]


def cluster(articles):
    """Group articles whose titles are similar (same story, different outlets)."""
    groups = []
    for a in articles:
        t = a["title"].lower()
        for g in groups:
            if difflib.SequenceMatcher(None, t, g[0]["title"].lower()).ratio() > 0.55:
                g.append(a)
                break
        else:
            groups.append([a])
    # Prefer stories covered by several different outlets.
    groups.sort(key=lambda g: len({x["source"]["name"] for x in g}), reverse=True)
    return groups


def summarize(client, group):
    text = "\n".join(
        f"- {a['source']['name']}: {a['title']}. {a.get('description') or ''}" for a in group[:6]
    )
    r = client.chat.completions.create(
        model=MODEL,
        response_format={"type": "json_object"},
        messages=[{"role": "user", "content":
            "Using ONLY these headlines/descriptions from different outlets, return JSON with keys "
            "title (neutral headline), sum (1-2 neutral sentences), more (3-4 sentences noting where "
            "outlets differ in framing). Do not add facts that are not in the text. JSON only.\n" + text}],
    )
    d = json.loads(r.choices[0].message.content)
    return {"title": d["title"], "sum": d["sum"], "more": d.get("more", "")}


def main():
    client = OpenAI()
    stories = []
    for cat, q in QUERIES.items():
        try:
            groups = cluster(fetch(q))[:5]
        except Exception as e:
            print(f"Skipping {cat}: {e}")
            continue
        for g in groups:
            try:
                s = summarize(client, g)
            except Exception as e:
                print(f"Summary failed in {cat}: {e}")
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

    if not stories:
        print("No stories built; leaving existing stories.json alone.")
        return
    with open("stories.json", "w") as f:
        json.dump({"updated": datetime.datetime.now().strftime("%b %d, %H:%M"),
                   "stories": stories}, f, indent=1)
    print(f"Wrote {len(stories)} stories.")


if __name__ == "__main__":
    main()
