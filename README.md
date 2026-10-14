# Shortlister: Freelance Demo Machine

Finds small, beginner-friendly web gigs, ranks the ten easiest, lets you shortlist some, has two
Ollama Cloud agents build a FastAPI + Jinja2 proof-of-concept for each one, and deploys them all to
Vercel. You get back a live URL for each gig that you can link in your proposal.

```
search feeds ─► rank 1-10 ─► you pick "1, 4, 7" ─► Architect ─► Developer ─► validate/repair ─► vercel deploy ─► live URLs
```

## Setup

Requires Python 3.11+, and Node.js for the Vercel CLI (`npm i -g vercel`; if it's missing, `npx` is used instead).

```bash
python -m venv .venv
.venv\Scripts\activate            # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env              # then fill in the keys
```

| Variable | Purpose |
|---|---|
| `OLLAMA_API_KEY` | Ollama Cloud key ([ollama.com/settings/keys](https://ollama.com/settings/keys)) |
| `OLLAMA_MODELS` | Fallback chain, largest first. Default `kimi-k2.7-code,gpt-oss:120b,gpt-oss:20b` |
| `VERCEL_TOKEN` | Access token for **your own** Vercel account ([vercel.com/account/tokens](https://vercel.com/account/tokens)) |
| `VERCEL_SCOPE` | Optional team slug |
| `FDM_RSS_FEEDS` | Optional comma-separated RSS/Atom gig feeds to scan in addition to Freelancer.com |

Other `FDM_*` settings are listed in [.env.example](.env.example).

## Usage

```bash
python -m fdm                      # full loop: search, rank, interactive pick, generate, deploy
python -m fdm --rank-only          # just print and save the top-10 table
python -m fdm --select "1,4,7"     # non-interactive pick
python -m fdm --skip-deploy        # generate into output/ only
python -m fdm deploy               # deploy every project already in output/
```

Outputs:

- `output/top10.md` / `top10.json`: the ranked table plus gig links and score breakdowns
- `output/<project_id>/`: one Vercel-ready app per shortlisted gig
- `output/deployments.md` / `deployments.json`: live URLs and failures

Preview a generated app locally with `cd output/<project_id>` and then `uvicorn index:app --reload`.

## Web portal

```bash
python -m fdm portal               # http://127.0.0.1:5000  (--port, --host, --top)
```

A Flask UI over the same pipeline:

1. **Shortlist** lists the saved top 10 from `output/top10.json` with scores, budgets, ranking
   signals and a link to each gig. **Refresh shortlist** searches again, which takes about 15 seconds.
   Gigs you have already built are tagged.
2. **Select** gigs by clicking their cards, choose whether to smoke-test and deploy to Vercel, then
   click **Create projects**.
3. The **job page** updates live as each gig moves through queued, generating, generated, deploying
   and live, or failed. It shows the model used, the output folder, live URLs, errors and build notes.

Jobs run one at a time in a background thread, while gigs within a job are built concurrently.
Job history is kept in memory only, but generated projects stay in `output/`. The Create button is
disabled until `OLLAMA_API_KEY` is set, and the deploy option until `VERCEL_TOKEN` is set. The portal
has no login: it binds to localhost by default, and every form is CSRF-protected because each POST
spends LLM tokens or deploys.

## How it works

| Phase | Module | What happens |
|---|---|---|
| 1. Aggregate | [fdm/sources.py](fdm/sources.py) | Playwright's HTTP client (no browser needed) queries the public Freelancer.com projects API, filtered to web/Python skills, for the latest gigs plus several keyword searches. It also reads any RSS/Atom feeds you configure. |
| 1. Rank | [fdm/ranking.py](fdm/ranking.py) | Scores each gig from 1 to 10. Velocity (landing pages, calculators, small APIs) counts 40%, independence (no payments, databases, auth, mobile, CMS) counts 35%, and clarity (specific, itemised, not sprawling) counts 25%. Gigs that aren't web builds, like SEO, ads or data entry, are damped. |
| 2. Generate | [fdm/agents.py](fdm/agents.py), [fdm/generator.py](fdm/generator.py) | **Agent A, the Architect**, turns the post into a JSON data contract: routes, template variables, UI sections and sample data. **Agent B, the Developer**, writes `index.py` and `templates/index.html` from it. |
| 2. Guardrails | [fdm/validate.py](fdm/validate.py) | Static checks come first: an import allowlist, no `eval`/`open`/dunder access, a `FastAPI` app is present, and the Jinja template parses with no `static/` or `extends`. Then a smoke test imports the app in a subprocess and calls every parameter-free route. Any failure goes back to the Developer, up to `FDM_MAX_REPAIRS` times. If it still fails, a deterministic fallback demo ships instead, so every gig gets a working URL. |
| 2. LLM client | [fdm/llm.py](fdm/llm.py) | Calls Ollama Cloud `/api/chat`. Rate-limit or token-limit errors fall back to the next, smaller model. 5xx errors and unusable replies are retried. A bad API key stops the run. |
| 3. Auth | [fdm/deploy.py](fdm/deploy.py) | Checks `VERCEL_TOKEN` against the Vercel API and locates the CLI *before* any LLM tokens are spent. |
| 4. Deploy | [fdm/deploy.py](fdm/deploy.py) | Writes a fresh `@vercel/python` `vercel.json` into each project and runs `vercel deploy --yes --prod`. It parses the deployment URL from the output and resolves the public `*.vercel.app` production alias. |

Each generated project looks like this:

```
output/<project_id>/
├── index.py              # FastAPI app; templates via Path(__file__).parent / "templates"
├── vercel.json           # @vercel/python build + catch-all route
├── requirements.txt      # fastapi, jinja2, python-multipart
├── templates/index.html  # single self-contained template, Tailwind CDN
├── .vercelignore
└── .fdm/gig.json         # source gig, score, blueprint, build notes (not uploaded)
```

## Notes

- **Deployment uses your own Vercel account token.** The tool does not create accounts with
  disposable email addresses. That violates Vercel's terms, and its signup flow is protected
  against automation.
- Vercel's Hobby plan is for non-commercial use. If you are using demos to win paid work, check
  whether you need a Pro plan.
- The smoke test runs LLM-generated code on your machine, though only after the static allowlist
  has passed it. Pass `--no-smoke` to skip that step; the static checks still run.
- Freelancer.com is the built-in source because it offers a public, unauthenticated API. Many other
  marketplaces forbid scraping or sit behind bot protection. Add any compliant feed through
  `FDM_RSS_FEEDS`.

## Tests

```bash
pytest
```
