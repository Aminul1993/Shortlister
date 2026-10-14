# Shortlister

Finds small, beginner-friendly web gigs, ranks the ten easiest, and lets you shortlist some. For each
one, two Ollama Cloud agents design and write a proof-of-concept web page. The page is rendered in
headless Chromium and saved as a screenshot. A third agent then writes the proposal that goes with it: a
short pitch tailored to the gig, built around what the screenshot shows, with a suggested bid. The
screenshot and the proposal are kept. The HTML exists in a temporary file only while it is rendered,
and no project is created. You can regenerate the screenshot or the proposal separately. Each run saves a
new version, and older versions are kept.

```
search feeds ─► rank 1-10 ─► you pick "1, 4, 7" ─► Architect ─► Developer ─► check/render/repair ─► screenshot PNG
                                                                                                                │
                                                                                 proposal .txt ◄─ Pitch Writer ◄┘
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
| `FDM_FREELANCER_NAME` | Optional name to sign proposals with |
| `FDM_FREELANCER_PROFILE` | Optional true facts about you (skills, past work) that proposals may mention. The Pitch Writer never invents experience, so without this it argues from the demo and the delivery plan alone |

Other `FDM_*` settings are listed in [.env.example](.env.example).

## Usage

Everything happens in a local web portal:

```bash
python -m fdm                      # http://127.0.0.1:5000  (--port, --host, --top, -v)
```

1. **Shortlist** lists the saved top 10 from `output/top10.json` with scores, budgets, ranking
   signals and a link to each gig. **Refresh shortlist** searches again, which takes about 15 seconds.
   Gigs that already have a screenshot or a proposal are tagged with their latest version, such as
   `screenshot v2`. The tag links to the gig's version history.
2. **Select** gigs by clicking their cards, then click one of the following:
   - **Create screenshots & proposals** makes a new screenshot and a proposal written for it.
   - **Screenshot only** makes a new screenshot and leaves the proposals alone.
   - **Proposal only** writes a new proposal for the gig's latest screenshot. If the gig has no
     screenshot, the proposal is written without one and doesn't mention one. It does not start the browser.
3. The **job page** updates live as each gig moves through queued, generating and done, or failed.
   It shows the screenshot and, under it, the proposal with a **Copy** button, its word count, a
   suggested bid and delivery time. It also shows the model used, the version numbers, the file paths,
   errors and build notes.
4. The **version history** page (`/demos/<demo_id>`, linked from the tags and the job page) lists
   every saved proposal and screenshot of a gig, newest first. It has buttons to make a new screenshot,
   a new proposal, or both. It warns you when the latest proposal describes an older screenshot than
   the latest one, or mentions no screenshot even though there is one it could describe.
5. **Send**: paste the proposal into the bid form, attach the screenshot it was written for (if any),
   and enter the bid. Read it first: it is a draft in your voice, and you are the one sending it.

Jobs run one at a time in a background thread, while gigs within a job are built concurrently.
Job history is kept in memory only, but every version of every screenshot and proposal stays in
`output/`. The create buttons are disabled until `OLLAMA_API_KEY` is set. The portal has no login: it binds to localhost by default,
and every form is CSRF-protected because each POST spends LLM tokens or queries the gig feeds.

Outputs:

- `output/top10.md` / `top10.json`: the ranked table plus gig links and score breakdowns
- `output/screenshots/<demo_id>.png`: a full-page screenshot (1280px wide) of the gig's demo page.
  Later versions are saved as `<demo_id>-v2.png`, `<demo_id>-v3.png` and so on.
- `output/proposals/<demo_id>.txt`: the proposal for that gig as plain text, ready to paste. Later
  versions are numbered the same way.
- A `.json` file beside each screenshot and proposal records how it was made. For a screenshot, that is
  the design brief, the model and the build notes. For a proposal, it is the screenshot version it
  describes (none if written without one), the suggested bid and the delivery time. Files saved before
  versioning have no `.json` and count as version 1. A screenshot without a saved brief can't be
  described, so **Proposal only** treats such a gig as having no screenshot.

## How it works

| Phase | Module | What happens |
|---|---|---|
| 1. Aggregate | [fdm/sources.py](fdm/sources.py) | Playwright's HTTP client queries the public Freelancer.com projects API, filtered to web/Python skills, for the latest gigs plus several keyword searches. It also reads any RSS/Atom feeds you configure. |
| 1. Rank | [fdm/ranking.py](fdm/ranking.py) | Scores each gig from 1 to 10. Velocity (landing pages, calculators, small APIs) counts 40%, independence (no payments, databases, auth, mobile, CMS) counts 35%, and clarity (specific, itemised, not sprawling) counts 25%. Gigs that aren't web builds, like SEO, ads or data entry, are damped. |
| 2. Generate | [fdm/agents.py](fdm/agents.py), [fdm/generator.py](fdm/generator.py) | **Agent A, the Architect**, turns the post into a JSON design brief: title, tagline, page sections and sample data. **Agent B, the Developer**, writes one static HTML page from it with Tailwind and hard-coded sample data, showing the core feature mid-use. |
| 2. Checks | [fdm/validate.py](fdm/validate.py), [fdm/screenshot.py](fdm/screenshot.py) | The page must be a complete document with no leftover template syntax. It is then rendered, and requests to hosts other than the Tailwind and Google Fonts CDNs are blocked. Blocked requests, missing local files, JavaScript errors and blank pages are reported. Any problem goes back to the Developer, up to `FDM_MAX_REPAIRS` times. If problems remain, the last draft that rendered real content is kept. If no draft did, a deterministic fallback page is rendered instead, so every gig gets a screenshot. |
| 2. Screenshot | [fdm/screenshot.py](fdm/screenshot.py), [fdm/versions.py](fdm/versions.py) | Each page is written to a temporary `.html` file, opened in headless Chromium at 1280×800 and captured full-page. The temporary file is deleted. Only the PNG is kept, saved as the gig's next screenshot version along with the design brief it was built from. Each portal job that makes screenshots starts Chromium *before* any LLM tokens are spent, so a missing browser fails the job straight away. |
| 2. Proposal | [fdm/agents.py](fdm/agents.py), [fdm/generator.py](fdm/generator.py) | **Agent C, the Pitch Writer**, runs once the screenshot is saved, so it describes the screenshot that was actually kept. It gets the job post, the design brief, the client's budget and your `FDM_FREELANCER_*` facts. It writes 120-200 words of plain text in your voice. The pitch opens on the client's goal, makes the screenshot its centrepiece and calls it a mockup with sample data. It then answers the 2-3 requirements that matter most, gives a delivery plan, and ends on one question that is easy to answer. It follows the post's own bid instructions, such as a code word. If the screenshot is the fallback outline page, the pitch says so instead of claiming the feature works. Replies with placeholders like `[Your Name]` are rejected and retried. The suggested bid is kept inside the client's budget. If the Pitch Writer fails, the screenshot is still kept and the build notes say why. **Proposal only** runs just this agent. It uses the brief saved with the latest screenshot, so the new proposal describes that exact screenshot. If there is no such screenshot, the agent is told nothing is attached. It then makes the pitch concrete by describing how the finished build will work, and never mentions a screenshot, mockup or demo. |
| 2. LLM client | [fdm/llm.py](fdm/llm.py) | Calls Ollama Cloud `/api/chat`. Rate-limit or token-limit errors fall back to the next, smaller model. 5xx errors and unusable replies are retried. A bad API key stops the run. |

## Notes

- Rendering runs LLM-written HTML and JavaScript in headless Chromium on your machine. The page is
  opened from a temporary file in its own browser context, and its network access is limited to the
  Tailwind and Google Fonts CDNs.
- The pages load Tailwind from its CDN, so rendering needs internet access. If the CDN can't be
  reached, the screenshot is still saved and the build notes say it may be unstyled.
- Suggested bids are in US dollars, converted from the gig's own currency, so convert back when the
  bid form uses another currency. A bid is only suggested when the gig states a budget.
- Freelancer.com is the built-in source because it offers a public, unauthenticated API. Many other
  marketplaces forbid scraping or sit behind bot protection. Add any compliant feed through
  `FDM_RSS_FEEDS`.

## Tests

```bash
pytest
```

The tests in `tests/test_screenshot.py` render real pages and are skipped if Chromium isn't installed.
