# Shortlister

Finds small, beginner-friendly web gigs, ranks the ten easiest, and lets you shortlist some. For each
one, two Ollama Cloud agents design and write a proof-of-concept web page. The page is rendered in
headless Chromium and saved as a screenshot you can attach to your proposal. Only the PNG is kept: the
HTML exists in a temporary file while it is rendered, and no project is created.

```
search feeds ─► rank 1-10 ─► you pick "1, 4, 7" ─► Architect ─► Developer ─► check/render/repair ─► screenshot PNGs
```

## Setup

Requires Python 3.11+.

```bash
python -m venv .venv
.venv\Scripts\activate            # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
cp .env.example .env              # then fill in the key
```

| Variable | Purpose |
|---|---|
| `OLLAMA_API_KEY` | Ollama Cloud key ([ollama.com/settings/keys](https://ollama.com/settings/keys)) |
| `OLLAMA_MODELS` | Fallback chain, largest first. Default `kimi-k2.7-code,gpt-oss:120b,gpt-oss:20b` |
| `FDM_RSS_FEEDS` | Optional comma-separated RSS/Atom gig feeds to scan in addition to Freelancer.com |

Other `FDM_*` settings are listed in [.env.example](.env.example).

## Usage

Everything happens in a local web portal:

```bash
python -m fdm                      # http://127.0.0.1:5000  (--port, --host, --top, -v)
```

1. **Shortlist** lists the saved top 10 from `output/top10.json` with scores, budgets, ranking
   signals and a link to each gig. **Refresh shortlist** searches again, which takes about 15 seconds.
   Gigs that already have a screenshot are tagged with a link to it.
2. **Select** gigs by clicking their cards, then click **Create screenshots**.
3. The **job page** updates live as each gig moves through queued, generating and captured, or
   failed. It shows the screenshot, the model used, the file path, errors and build notes.

Jobs run one at a time in a background thread, while gigs within a job are built concurrently.
Job history is kept in memory only, but screenshots stay in `output/screenshots/`. The Create button
is disabled until `OLLAMA_API_KEY` is set. The portal has no login: it binds to localhost by default,
and every form is CSRF-protected because each POST spends LLM tokens or queries the gig feeds.

Outputs:

- `output/top10.md` / `top10.json`: the ranked table plus gig links and score breakdowns
- `output/screenshots/<demo_id>.png`: one full-page screenshot (1280px wide) per shortlisted gig

## How it works

| Phase | Module | What happens |
|---|---|---|
| 1. Aggregate | [fdm/sources.py](fdm/sources.py) | Playwright's HTTP client queries the public Freelancer.com projects API, filtered to web/Python skills, for the latest gigs plus several keyword searches. It also reads any RSS/Atom feeds you configure. |
| 1. Rank | [fdm/ranking.py](fdm/ranking.py) | Scores each gig from 1 to 10. Velocity (landing pages, calculators, small APIs) counts 40%, independence (no payments, databases, auth, mobile, CMS) counts 35%, and clarity (specific, itemised, not sprawling) counts 25%. Gigs that aren't web builds, like SEO, ads or data entry, are damped. |
| 2. Generate | [fdm/agents.py](fdm/agents.py), [fdm/generator.py](fdm/generator.py) | **Agent A, the Architect**, turns the post into a JSON design brief: title, tagline, page sections and sample data. **Agent B, the Developer**, writes one static HTML page from it with Tailwind and hard-coded sample data, showing the core feature mid-use. |
| 2. Checks | [fdm/validate.py](fdm/validate.py), [fdm/screenshot.py](fdm/screenshot.py) | The page must be a complete document with no leftover template syntax. It is then rendered, and requests to hosts other than the Tailwind and Google Fonts CDNs are blocked. Blocked requests, missing local files, JavaScript errors and blank pages are reported. Any problem goes back to the Developer, up to `FDM_MAX_REPAIRS` times. If problems remain, the last draft that rendered real content is kept. If no draft did, a deterministic fallback page is rendered instead, so every gig gets a screenshot. |
| 2. Screenshot | [fdm/screenshot.py](fdm/screenshot.py) | Each page is written to a temporary `.html` file, opened in headless Chromium at 1280×800 and captured full-page. The temporary file is deleted, and only `output/screenshots/<demo_id>.png` is kept. Each portal job starts Chromium *before* any LLM tokens are spent, so a missing browser fails the job straight away. |
| 2. LLM client | [fdm/llm.py](fdm/llm.py) | Calls Ollama Cloud `/api/chat`. Rate-limit or token-limit errors fall back to the next, smaller model. 5xx errors and unusable replies are retried. A bad API key stops the run. |

## Notes

- Rendering runs LLM-written HTML and JavaScript in headless Chromium on your machine. The page is
  opened from a temporary file in its own browser context, and its network access is limited to the
  Tailwind and Google Fonts CDNs.
- The pages load Tailwind from its CDN, so rendering needs internet access. If the CDN can't be
  reached, the screenshot is still saved and the build notes say it may be unstyled.
- Freelancer.com is the built-in source because it offers a public, unauthenticated API. Many other
  marketplaces forbid scraping or sit behind bot protection. Add any compliant feed through
  `FDM_RSS_FEEDS`.

## Tests

```bash
pytest
```

The tests in `tests/test_screenshot.py` render real pages and are skipped if Chromium isn't installed.
