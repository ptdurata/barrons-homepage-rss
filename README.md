# barrons-homepage-rss

**Feed URL:** https://ptdurata.github.io/barrons-homepage-rss/barrons.xml

> **Status: BLOCKED by robots.txt (2026-10-18). No feed is published.**
> Barron's `robots.txt` disallows the whole site for every crawler that isn't
> on its allowlist, including this one. The scraper honors that, so it has
> never fetched the homepage, and the hourly schedule is switched off. See
> [Verification status](#verification-status).

An unofficial RSS 2.0 feed of the headlines and links shown on the public
[Barron's homepage](https://www.barrons.com/). A GitHub Actions job builds it
every hour and GitHub Pages serves it. There is no server, no proxy, no
Cloudflare, and no credentials or Barron's login anywhere in the pipeline.

Not affiliated with Barron's or Dow Jones.

## What gets published

For each story linked from the homepage, the feed contains only what the
homepage itself shows:

| RSS field     | Source                                                               |
|---------------|----------------------------------------------------------------------|
| `title`       | Homepage headline text                                               |
| `link`        | Article URL, with tracking query strings (`?mod=...`) removed        |
| `description` | Homepage summary or teaser, if one is shown (capped at 300 characters) |
| `pubDate`     | Publication date, if the homepage exposes one (otherwise left out)   |
| `guid`        | The canonical article URL (`isPermaLink="true"`), stable across runs |

Article pages are **never fetched**. No article body text, subscriber-only
content, cookies or tokens are downloaded, stored or republished. Each run
makes two requests: `robots.txt` and the homepage.

## Schedule

`.github/workflows/update.yml` runs at the top of every hour (`cron: "0 * * * *"`),
and you can also start it by hand. GitHub may delay scheduled runs by several
minutes when it's busy, and it pauses scheduled workflows in public repos after
60 days with no repository activity. The feed's own commits count as activity
when they happen.

## How the parser works

1. **Politeness checks.** `scraper.py` fetches `https://www.barrons.com/robots.txt`
   and stops if it disallows the homepage for this project's User-Agent:
   `barrons-homepage-rss/1.0 (+https://github.com/ptdurata/barrons-homepage-rss; public headline RSS feed)`.
2. **One homepage request.** Plain `requests` with a 20-second timeout, with no
   browser automation, cookies or session.
3. **Block detection.** HTTP 401/403/429, or an anti-bot interstitial (DataDome,
   PerimeterX, Cloudflare challenge markers), makes the run stop and fail. **No
   workaround is attempted:** no CAPTCHA solving, proxy rotation, browser
   spoofing or credentials.
4. **Extraction.** Several public sources inside the same HTML are read, then merged:
   - **Rendered HTML anchors** that point to `/articles/...` or `/livecoverage/...`
     on barrons.com, with the headline taken from the link or heading text. The
     homepage HTML order follows editorial placement, so it sets the item order
     when it yields enough stories.
   - **Embedded JSON:** Next.js `__NEXT_DATA__`, other `application/json`
     scripts, and JSON-LD. The parser walks the JSON for objects that have an
     article URL plus a headline, and picks up `summary`/`description` and
     `datePublished`-style fields when present. If the HTML yields too few
     stories, this data leads instead.
   - Matching keys by name (instead of hard-coding one JSON path) is meant to
     survive minor layout changes.
5. **Deduplication** by canonical URL (`https://www.barrons.com/<path>`, with
   the query and fragment stripped).
6. **Credibility gate.** At least 5 articles with real-looking headlines are
   required; otherwise the run fails.
7. **RSS output.** Built with `xml.etree.ElementTree`, which XML-escapes every
   text field, then re-parsed and checked structurally: required channel
   fields, title and link on every item, unique GUIDs, and parseable
   `pubDate`s.
8. **No-churn, atomic write.** If the items are identical to the current
   `barrons.xml`, the file isn't touched, so no commit happens. Otherwise the
   new file is written to a temp file and atomically renamed into place.

Any failure exits with code 1 **before** `barrons.xml` is touched, so the last
good feed stays published.

## Run locally

```bash
git clone https://github.com/ptdurata/barrons-homepage-rss.git
cd barrons-homepage-rss
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt

python -m pytest -q                 # offline tests (synthetic fixtures)
python scraper.py -o barrons.xml -v # live scrape
python scraper.py --from-file saved_homepage.html -o /tmp/test.xml  # parse a saved page
```

## Run the workflow manually

GitHub → **Actions** → **Update Barron's feed** → **Run workflow** (branch `main`).
From the command line: `gh workflow run update.yml -R ptdurata/barrons-homepage-rss`.

## Validate the RSS output

```bash
# Well-formed XML
python -c "import xml.etree.ElementTree as E; E.parse('barrons.xml'); print('ok')"
# or: xmllint --noout barrons.xml

# Structural RSS 2.0 checks used by the scraper
python -c "import scraper; print(scraper.validate_rss(open('barrons.xml','rb').read()), 'items')"

# Live feed
curl -sSI https://ptdurata.github.io/barrons-homepage-rss/barrons.xml | head -1
```

For a third-party check, paste the feed URL into the
[W3C Feed Validator](https://validator.w3.org/feed/).

## Known failure modes

- **Anti-bot blocking.** Dow Jones sites use bot-mitigation services. Requests
  from GitHub-hosted runner IPs may get a 401/403 or a CAPTCHA page. The run
  then fails visibly (red X in Actions) and the previous feed stays. By design
  this project does **not** try to evade blocking. If blocking becomes
  persistent, the feed stops updating; see the maintenance notes below.
- **Markup or data changes.** If Barron's renames its JSON fields or changes
  article URL patterns (`/articles/`, `/livecoverage/`), extraction can drop
  below 5 stories and the run fails without overwriting the feed.
- **Missing dates or summaries.** Some homepage cards have no teaser or
  timestamp. Those items simply leave out `description` or `pubDate`, and feed
  readers then usually show the time they first saw the item.
- **Scheduler delays.** GitHub cron isn't exact, and it pauses on inactive
  repos (see above).
- **Pages lag.** GitHub Pages usually republishes within a minute or two of a
  commit, but this can take longer.

## robots.txt and terms of use

- The scraper checks `robots.txt` at runtime on every run and refuses to fetch
  the homepage if it's disallowed for this User-Agent.
- Barron's is published by Dow Jones. Its Terms of Use / Subscriber Agreement
  (linked in the footer of barrons.com) restrict automated access and
  redistribution of its content. This project limits itself to headline text
  and links that are publicly displayed, makes one homepage request per hour,
  identifies itself honestly, and never touches article text or paywalled
  content. **Even so, the operator of this repo should review the current
  Dow Jones terms and decide whether this use is acceptable;** headline
  aggregation may still be outside what the terms allow. If Barron's or Dow
  Jones objects, disable the workflow.
- Barron's headlines are copyrighted by Dow Jones & Company. This feed links
  back to the original articles.

## Verification status

Two manual runs on 2026-10-18 from GitHub-hosted runners
([run 1](https://github.com/ptdurata/barrons-homepage-rss/actions/runs/39148631096),
[run 2](https://github.com/ptdurata/barrons-homepage-rss/actions/runs/39148772241))
both stopped at the robots.txt check, before the homepage was requested:

```
INFO robots.txt HTTP 200, 3585 bytes; homepage allowed: False
INFO robots.txt: User-agent: *
INFO robots.txt: Disallow: /
INFO robots.txt: User-agent: googlebot
...  (about 80 named crawlers: search engines, social link previews, SEO and monitoring tools)
INFO robots.txt: Allow: /
...
ERROR robots.txt disallows the homepage for this User-Agent
```

`https://www.barrons.com/robots.txt` is an **allowlist**. The `*` group
disallows `/`, and only the named user agents get `Allow: /`. The only way
past it is to claim to be one of the listed crawlers. That's User-Agent
spoofing, which this project rules out, so:

- `barrons.xml` has never been generated, and the feed URL returns 404.
- The homepage structure (raw HTML vs. `__NEXT_DATA__` vs. GraphQL) could not
  be inspected, because doing so would itself break the robots.txt rule. The
  parser's multi-source extraction is tested only against synthetic fixtures.
- The hourly `schedule` trigger is commented out in
  `.github/workflows/update.yml` so the job doesn't fail every hour. Manual
  runs still work and will still refuse while the rule is in place.

To revive the project, Barron's would need to allow this User-Agent (or all
agents) in robots.txt, or give explicit permission. Then re-enable the
`schedule` block and run the workflow manually to verify.

## Files

| File                          | Purpose                                         |
|-------------------------------|-------------------------------------------------|
| `scraper.py`                  | Fetch, extract, validate and write `barrons.xml` |
| `tests/test_scraper.py`       | Offline tests using synthetic HTML              |
| `.github/workflows/update.yml`| Hourly and manual job; commits the feed if it changed |
| `barrons.xml`                 | The published feed (generated)                  |
| `.nojekyll`                   | Serve files as-is on GitHub Pages               |
