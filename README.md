# Shortlister

Finds small, beginner-friendly web gigs, ranks the ten easiest, and lets you shortlist some. For each
one, an Ollama Cloud agent writes a proposal: a short pitch tailored to the gig, with a suggested bid
and delivery time. Each run saves a new version of the proposal, and older versions are kept. When
you're happy with a proposal, you can review it beside the live Freelancer.com project and place the
bid from the portal through Freelancer's official SDK.

```
search feeds ─► rank 1-10 ─► you pick "1, 4, 7" ─► Pitch Writer ─► proposal .txt ─► you review ─► bid on Freelancer.com
```

## Setup

Requires Python 3.11+.

```bash
python -m venv .venv
.venv\Scripts\activate            # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env              # then fill in the key
```

| Variable | Purpose |
|---|---|
| `OLLAMA_API_KEY` | Ollama Cloud key ([ollama.com/settings/keys](https://ollama.com/settings/keys)) |
| `OLLAMA_MODELS` | Fallback chain, largest first. Default `kimi-k2.7-code,gpt-oss:120b,gpt-oss:20b` |
| `FDM_RSS_FEEDS` | Optional comma-separated RSS/Atom gig feeds to scan in addition to Freelancer.com |
| `FDM_FREELANCER_NAME` | Optional name to sign proposals with |
| `FDM_FREELANCER_PROFILE` | Optional true facts about you (skills, past work) that proposals may mention. The Pitch Writer never invents experience, so without this it argues from the delivery plan alone |
| `FREELANCER_OAUTH_TOKEN` | Optional OAuth token for your Freelancer.com account, from the [developer portal](https://developers.freelancer.com). With it, the portal can place bids for you. Anyone with the token can bid as you |
| `FDM_HOST` / `FDM_PORT` | Where the portal listens. Default `127.0.0.1` and `5000` |
| `FDM_SHORTLIST_SIZE` | How many gigs a refresh keeps. Default `10` |
| `FDM_LOG_LEVEL` | `DEBUG`, `INFO` (default), `WARNING` or `ERROR` |

Other `FDM_*` settings are listed in [.env.example](.env.example).

## Usage

Everything happens in a local web portal. Start the server from the project folder:

```bash
python app.py                      # http://127.0.0.1:5000
```

There are no command-line options: everything is configured in `.env` or environment variables.
`app.py` reads `.env` and writes `output/` in its own folder, wherever you start it from.
The portal runs on [Waitress](https://docs.pylonsproject.org/projects/waitress/), a production WSGI
server that works on Windows, macOS and Linux. Run it as a single process, because jobs and their
progress live in that process's memory. Stop it with Ctrl+C.

1. **Shortlist** lists the saved top 10 from `output/top10.json` with scores, budgets, ranking
   signals and a link to each gig. **Refresh shortlist** searches again, which takes about 15 seconds.
   Gigs that already have a proposal are tagged with its latest version, such as `proposal v2`, and
   with the bid and delivery time suggested with it, such as `bid ~$180 · 4 days`. The tags link to
   the gig's version history.
2. **Select** gigs by clicking their cards, then click **Write proposals**.
3. The **job page** updates live as each gig moves through queued, generating and done, or failed.
   It shows each proposal with a **Copy** button, its word count, a suggested bid and delivery time,
   the model that wrote it, its version number and file path, and any errors.
4. The **version history** page (`/demos/<demo_id>`, linked from the tags and the job page) lists
   every saved proposal of a gig, newest first, and has a **New proposal** button.
5. **Send**: on the version history page of a Freelancer.com gig, click **Review & place bid**. The bid page
   looks the project up live and shows the account the bid comes from, whether the project still takes bids,
   its budget in its own currency and how many bids it has. The form is filled in with the latest proposal,
   its suggested bid converted to the project's currency, and its delivery time. Edit anything, then click
   **Place bid on Freelancer.com**. Nothing is sent before that click. If Freelancer.com refuses the bid,
   its reason is shown above the form and your edits are kept. A placed bid is listed on the version history
   page and tagged `bid placed` on the shortlist. Freelancer.com takes one bid per project, so the portal
   won't send a second one. To change a bid, edit it on the gig page.
   Read the proposal first. It is a draft in your voice, and you are the one sending it.

Jobs run one at a time in a background thread, while gigs within a job are written concurrently.
Job history is kept in memory only, but every version of every proposal stays in `output/`. The
**Write proposals** button is disabled until `OLLAMA_API_KEY` is set, and bidding needs
`FREELANCER_OAUTH_TOKEN`. The portal has no login: it binds to localhost by default, and every form is
CSRF-protected because each POST spends LLM tokens, queries the gig feeds or places a bid.

Outputs:

- `output/top10.md` / `top10.json`: the ranked table plus gig links and score breakdowns
- `output/proposals/<demo_id>.txt`: the proposal for that gig as plain text, ready to paste. Later
  versions are saved as `<demo_id>-v2.txt`, `<demo_id>-v3.txt` and so on.
- A `.json` file beside each proposal records the model that wrote it, the suggested bid and the
  delivery time. Files saved before versioning have no `.json` and count as version 1.
- `output/bids/<demo_id>.json`: every bid placed on that gig from the portal. Each entry has the bid id,
  amount, currency, delivery days, upfront milestone, the proposal version it started from and the exact
  text sent.

## How it works

| Phase | Module | What happens |
|---|---|---|
| 1. Aggregate | [fdm/sources.py](fdm/sources.py) | Playwright's HTTP client queries the public Freelancer.com projects API, filtered to web/Python skills, for the latest gigs plus several keyword searches. It also reads any RSS/Atom feeds you configure. No browser is launched. |
| 1. Rank | [fdm/ranking.py](fdm/ranking.py) | Scores each gig from 1 to 10. Velocity (landing pages, calculators, small APIs) counts 40%, independence (no payments, databases, auth, mobile, CMS) counts 35%, and clarity (specific, itemised, not sprawling) counts 25%. Gigs that aren't web builds, like SEO, ads or data entry, are damped. |
| 2. Proposal | [fdm/agents.py](fdm/agents.py), [fdm/generator.py](fdm/generator.py), [fdm/versions.py](fdm/versions.py) | **The Pitch Writer** gets the job post, the client's budget and your `FDM_FREELANCER_*` facts. It writes 120-200 words of plain text in your voice. The pitch opens on the client's goal, then makes it concrete by describing how the finished build will work for them. It answers the 2-3 requirements that matter most, gives a delivery plan, and ends on one question that is easy to answer. It follows the post's own bid instructions, such as a code word, and never mentions a screenshot, mockup or attachment. Replies with placeholders like `[Your Name]` are rejected and retried. Every proposal comes with a suggested bid and delivery time. The bid is kept inside the client's budget, or is the agent's estimate for the scope when the gig states none. The proposal is saved as the gig's next version. |
| 2. LLM client | [fdm/llm.py](fdm/llm.py) | Calls Ollama Cloud `/api/chat`. Rate-limit or token-limit errors fall back to the next, smaller model. 5xx errors and unusable replies are retried. A bad API key stops the run. |
| 3. Bid | [fdm/bids.py](fdm/bids.py) | Uses [freelancersdk](https://github.com/freelancer/freelancer-sdk-python), Freelancer.com's official Python SDK. The bid page calls `get_self` for the account and `get_project_by_id` for the project's live state. Placing a bid checks both again, refuses a project that no longer takes bids, then calls `place_project_bid` with the amount, delivery days, upfront milestone percentage and text from the form. SDK errors, unreadable replies, network failures and requests that take over 30 seconds become one message that names the step that failed. |

## Notes

- Suggested bids are in US dollars, converted from the gig's own currency. The bid page converts them
  back at Freelancer's current rate and keeps them inside the project's budget. When you bid on the gig
  page instead, convert back yourself if the bid form uses another currency. When the gig states no
  budget, the bid is the agent's own estimate of a fair price for the scope, so check it before sending.
- Freelancer's API sends a bid as text only. Proposals saved by earlier versions of Shortlister may refer
  to an attached screenshot; the bid page warns you about those so you can reword them.
- Only Freelancer.com gigs can be bid on from the portal. Gigs from `FDM_RSS_FEEDS` have no bid API.
- Freelancer.com is the built-in source because it offers a public, unauthenticated API. Many other
  marketplaces forbid scraping or sit behind bot protection. Add any compliant feed through
  `FDM_RSS_FEEDS`.

## Tests

```bash
pytest
```
