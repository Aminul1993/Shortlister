# Shortlister Codebase Review

**Date:** 2026-10-15 · **Commit reviewed:** `4181cc9` · **Scope:** the whole repository (`fdm/`, `tests/`, config and docs)

**Method.** Every tracked file was read. The test suite was run (58 passed) and measured for branch coverage (88%).
The 53 installed packages were checked against the OSV vulnerability database (0 known issues). Each security and
data-integrity finding below was reproduced with a scratch script, and the performance figures come from local
benchmarks on Windows 11 with Python 3.12.0. No repository code was changed for this review.

Much of a generic review checklist doesn't apply here: there's no ORM, no database and no frontend framework. Those
sections are marked N/A rather than padded with findings. This is a small, well-built codebase. What needs doing is a
handful of security and integrity fixes, not a rewrite.

# Executive Summary

All scores are out of 10, where 10 is best.

| Area | Score | Why |
|---|---|---|
| Overall code quality | **8** | Typed, small functions, docstrings that explain why, 88% branch coverage |
| Architecture | **7.5** | Clean, acyclic module graph. The web layer mixes routing with view logic, and bidding depends on the current shortlist |
| Maintainability | **7.5** | JSON shapes are untyped dicts spread across 4 modules, plus "demo" naming left over from the removed screenshot feature |
| Security | **6** | CSRF is solid and the default bind is localhost. But a tool that bids as you is open to DNS rebinding, clickjacking and `javascript:` links |
| Performance | **8** | Right for its scale. Some file scans grow with history, and the dependencies are heavy |
| Technical debt | **7** | Low to moderate, mostly naming and dependency hygiene |

# Repository Overview

- **Stack:** Python 3.11+ (reviewed on 3.12.0), Flask 3.1.3 with Jinja2 3.1.6, served by Waitress 3.0.2. LLM calls use
  httpx 0.28.1 against Ollama Cloud. Gig fetching uses Playwright 1.64 (only its HTTP client). Bidding uses
  freelancersdk 0.1.20 (which uses requests). The frontend is 71 lines of vanilla JS and 265 lines of CSS.
- **Size:** about 1,620 lines of Python in `fdm/` (940 statements). Tests are 865 lines, 58 tests.
- **Storage:** no database. Everything lives in files under `output/`:

| Data | Files | How it's written |
|---|---|---|
| Shortlist | `top10.json`, `top10.md` | Overwritten on each refresh |
| Proposal versions | `proposals/<id>[-vN].txt` + `.json` sidecar | New file per version, never overwritten |
| Bid log | `bids/<id>.json` | Read, append, rewrite |
| Jobs | in memory | Lost on restart |

`output/screenshots/` is left over from the removed screenshot feature; no code uses it.

**The three business flows:**
1. **Refresh:** `POST /refresh` → `fetch_gigs` (9 Freelancer queries plus RSS feeds, run concurrently) → `rank_gigs` →
   `save_shortlist`.
2. **Write proposals:** `POST /jobs` → JobManager queue (one worker thread) → `generate_proposal` → Pitch Writer →
   `OllamaCloud.complete` (falls back through smaller models) → `save_proposal`.
3. **Bid (the critical path, irreversible):** `GET /demos/<id>/bid` → `lookup` (account and project) → editable form →
   `POST` → CSRF check → lock → local duplicate check → `place_bid` → `save_bid`.

**Module dependencies (no cycles):**
```
server ─► web ─┬─► web.jobs ─► generator ─┬─► agents ─► llm(httpx)
               │                          ├─► versions
               │                          └─► store ─► ranking ─► models
               ├─► sources(playwright) ─► config, models
               ├─► bids(freelancersdk, requests) ─► store
               └─► store, versions, ranking, config
```

# Architecture Findings

| ID | Sev | Finding |
|---|---|---|
| AR1 | ✅ | Each pipeline phase maps to one module, with no cycles. Keep this. Clean Architecture layering would be over-engineering at about 1.6k lines. |
| AR2 | Med | [web/\_\_init\_\_.py](fdm/web/__init__.py) is one 210-line `create_app` closure. It holds 9 routes, form parsing and view-model building, so the logic can't be unit-tested apart from HTTP. |
| AR3 | Low | Dependencies point the wrong way: the store depends on the ranking module (`store` imports `render_table`, [store.py:13](fdm/store.py#L13)), and `bids` borrows `store.write_json`. `bid_label`, a display helper, lives in [jobs.py:50](fdm/web/jobs.py#L50). |
| AR4 | Med | Bidding and "New proposal" require the gig to be on the **current** shortlist ([web/\_\_init\_\_.py:70](fdm/web/__init__.py#L70), [:171](fdm/web/__init__.py#L171)). Once a refresh drops it, a gig that already has a proposal can't be bid on from the portal. **Needs a product decision.** |
| AR5 | Low | The sidecar and bid-record formats are untyped `dict`s. They're written in `generator`/`web` and read in `versions`, `jobs` and `web`. |

# Security Findings

| ID | Sev | Finding | Evidence |
|---|---|---|---|
| SEC1 | **High** | **DNS rebinding.** The portal answers to any `Host`. A malicious page whose domain later resolves to 127.0.0.1 becomes same-origin with it. It can then read a CSRF token from `/`, `POST /demos/<id>/bid` with its own text and amount, and the bid is placed **as you**. It can also start jobs that spend your tokens, or read your proposals and profile. Chrome's Local Network Access mitigates this only partly; other browsers don't. | `Host: attacker.example:5000` → 200, page contains a valid token. With Flask's `TRUSTED_HOSTS` → 400 (verified). |
| SEC2 | Med | **Clickjacking.** There's no `X-Frame-Options` or CSP `frame-ancestors`, and the session cookie has no `SameSite`. Another site can frame the pre-filled bid form and trick you into clicking "Place bid". Browsers without Lax-by-default cookies (Firefox, Safari) are exposed. | Response headers: none. `Set-Cookie: …; HttpOnly; Path=/` |
| SEC3 | Med | **`javascript:` links from feeds.** RSS/Atom links are stored as-is ([sources.py:107](fdm/sources.py#L107), [:113](fdm/sources.py#L113)) and rendered as `href` on all 4 pages. Script running in the portal's origin can place bids. This needs a malicious or compromised feed. | Rendered `href="javascript:alert(document.domain)"` (verified) |
| SEC4 | Low | Binding to a non-localhost address has no authentication. This is documented and logs a warning. | [server.py:34](fdm/server.py#L34) |
| SEC5 | Low | The `Settings` repr includes `ollama_api_key` and `freelancer_oauth_token`, so any debug log or pytest diff of the settings prints both secrets. | [config.py:40](fdm/config.py#L40) |
| SEC6 | Low | `FDM_PORTAL_SECRET` is read outside `Settings` and isn't documented. Each restart rotates the key, which expires every open form. | [web/\_\_init\_\_.py:56](fdm/web/__init__.py#L56) |

**Checked and OK:**
- CSRF protection on every POST, with a constant-time compare.
- Jinja autoescaping everywhere, and no `|safe`.
- Path traversal on `/proposals` and `/demos` (blocked by `safe_join` plus the `DEMO_ID` regex, and tested).
- Prompt injection: the job post is fenced as untrusted, a human reviews before sending, and the bid is clamped to the
  budget.
- XML parsing: the bundled expat is 2.5.0, which blocks entity-expansion (billion laughs) and external-entity (XXE)
  attacks.
- Secrets: `.env` is gitignored, and no secrets appear anywhere in git history.
- Not applicable: SQL, command injection, unsafe deserialization, SSRF (feeds are set by the operator, not by an
  attacker).

# Performance Findings

| ID | Sev | Finding | Measured |
|---|---|---|---|
| PERF1 | Low–Med | The index page globs `proposals/` once per row and reads the sidecar of every version, though only the latest is shown ([web/\_\_init\_\_.py:84-95](fdm/web/__init__.py#L84-L95), [versions.py:37](fdm/versions.py#L37)). Cost grows linearly with history. | `GET /`: **24.8 ms** at 60 files, **87.9 ms** at 1,260, **147.2 ms** at 6,060 |
| PERF2 | Med | Playwright is used only as an HTTP client. Each refresh starts a Node driver process, while httpx is already a dependency. | Client start and stop: 739 ms vs 536 ms median, so about **0.2 s per refresh**. The bigger win is install size: Playwright is 113 MB. |
| PERF3 | Med | There's no overall deadline per proposal. A request can take 300 s, with 2 tries per model, 3 models and a second round if rate-limited. That's about 30 to 60 minutes stuck in "generating", with no way to cancel. | [llm.py:91](fdm/llm.py#L91) |
| PERF4 | Low | `JobManager._jobs` grows without limit, and `recent()` deep-copies the history on every index load. | [jobs.py:100](fdm/web/jobs.py#L100) |
| PERF5 | Low | The job page keeps polling every 2 s even in hidden tabs. | [portal.js:51](fdm/web/static/portal.js#L51) |
| N/A | — | Bundle size, code splitting, re-renders and memoisation don't apply: the pages are server-rendered with no build step. Adding a framework would make things worse. | |

# Database Findings

There's no database, so this covers the file store.

| ID | Sev | Finding |
|---|---|---|
| DB1 | **Med (integrity)** | Writes aren't atomic. `write_json` writes in place ([store.py:31](fdm/store.py#L31)). Verified chain: a truncated `bids/<id>.json` makes `placed_bids()` return `[]`, which turns off the duplicate-bid guard. The next `save_bid` then overwrites the file with only the new record, so the bid history is gone. |
| DB2 | Med | If the bid is placed but `save_bid` fails, the user gets a 500 page and the exact text sent is lost ([web/\_\_init\_\_.py:233-238](fdm/web/__init__.py#L233-L238)). |
| DB3 | Low | A corrupt `top10.json` silently shows "Nothing shortlisted yet", with nothing logged ([store.py:65](fdm/store.py#L65)). |
| DB4 | Info | Version numbering is race-free only because a single worker thread writes proposals. That holds today; it should be documented as an invariant. |

Moving to SQLite isn't recommended now. The plain-text proposals are a user-facing feature and the volume is tiny.
Reconsider if the portal ever has several users or more than about 10k proposals.

# Dependency Findings

| ID | Finding |
|---|---|
| DEP1 | freelancersdk 0.1.20 sets no timeouts, which is why the code patches `session.request` ([bids.py:113](fdm/bids.py#L113)). It also lists `guzzle_sphinx_theme` as a runtime dependency, which pulls in Sphinx 9.1, Babel, docutils and 12 more packages: **66 MB measured, for 3 HTTP calls**. |
| DEP2 | Playwright (113 MB) is used only for GET requests (see PERF2). |
| DEP3 | Builds aren't reproducible. `requirements.txt` has only `>=` bounds and no lock file, and pytest is listed as a runtime dependency. `pyproject.toml` declares no dependencies and is still named `freelance-demo-machine`. |
| DEP4 | ✅ OSV found 0 known vulnerabilities in the 53 installed packages (checked 2026-10-15). |

# Code Quality Findings

| ID | Sev | Finding |
|---|---|---|
| CQ1 | **Med** | **Settings aren't validated.** `FDM_LLM_CONCURRENCY=0` creates `Semaphore(0)`, so every job hangs forever (verified). A non-numeric `FDM_PORT` or an unknown `FDM_LOG_LEVEL` crashes startup with a raw traceback ([config.py:33](fdm/config.py#L33)). |
| CQ2 | Low | The same "latest meta → bid label and delivery days" logic is repeated in 4 places: index, demo, `bid_page` and `jobs._build`. |
| CQ3 | Low | In [llm.py:195](fdm/llm.py#L195) the limit check runs before the 5xx check, and `_LIMIT_HINTS` matches any message containing "token" or "exceed". So a 502 saying "upstream timeout exceeded" triggers a model fallback plus a 20 s cooldown round instead of a retry. |
| CQ4 | Low | Naming left over from "Freelance Demo Machine": `demo_id`, `/demos/`, the `FreelanceDemoMachine` User-Agent ([sources.py:27](fdm/sources.py#L27)), the "FD" logo mark, the `mentions_screenshot` shim, and the unused CSS class `.warn-text`. |
| CQ5 | Low | Docs have drifted. The README's default `OLLAMA_MODELS` (`kimi-k2.7-code…`) differs from [.env.example:4](.env.example#L4). `python -m fdm` doesn't `chdir` the way [app.py](app.py) does. |
| CQ6 | Low | `create_app` starts a daemon thread on every call, so each test leaks one. |

# Technical Debt Analysis

| Debt | Cost if left | Effort |
|---|---|---|
| Security gaps (SEC1–3) | A malicious page could place bids as you | S |
| Non-atomic writes (DB1/2) | Bid history lost, duplicate guard off | S |
| Unvalidated settings (CQ1) | Silent hang, confusing crashes | S |
| Heavy dependencies (DEP1/2) | 179 MB, a Node process, an unmaintained SDK | M |
| Web layer concentration (AR2/CQ2) | Every new page adds to `create_app` | M |
| Untyped JSON formats (AR5) | Keys drift silently across 4 modules | M |
| Naming (CQ4) | Onboarding confusion | S (keep the URLs) |

# Prioritised Refactoring Plan

## Critical Issues (Batch 1, about half a day, one commit)

1. **SEC1:** set `TRUSTED_HOSTS` from `FDM_HOST`, with `FDM_TRUSTED_HOSTS` as an override.
2. **SEC2:** add CSP, `X-Frame-Options`, `nosniff`, `Referrer-Policy` and `SameSite=Lax`.
3. **SEC3:** accept only `http(s)` links from feeds.
4. **DB1/DB2:** make writes atomic, make the bid-log read strict, and handle "placed but not recorded".
5. **CQ1:** validate settings and fail at startup with a message naming the variable.

## High Impact Improvements

6. **PERF3:** one deadline per proposal for the whole fallback chain (default 10 min).
7. **PERF2/DEP2:** replace Playwright with httpx, and add fetch-path tests (that code is at 63% coverage).
8. **PERF1:** find each gig's latest proposal in a single directory scan.
9. **AR2/AR3/CQ2:** move formatting and view-model helpers into `fdm/web/format.py`, and stop `store` depending on
   `ranking`.
10. **AR5:** typed `ProposalMeta` and `BidRecord`, keeping the same JSON on disk.
11. **AR4 (needs a decision):** save a snapshot of the gig with each proposal, so it can still be bid on after leaving
    the shortlist.
12. **DEP1 (needs a decision):** replace freelancersdk with about 50 lines of httpx. The README currently promises the
    official SDK.

## Quick Wins

- Mark the secret fields in `Settings` with `repr=False`.
- Log when the shortlist file can't be read.
- Keep at most 50 jobs in memory.
- Pause polling while the tab is hidden.
- Check for 5xx before the limit hints in `llm.py`.
- Split out `requirements-dev.txt`, declare dependencies in `pyproject`, and add a lock file.
- Fix the doc drift and document `FDM_PORTAL_SECRET`.
- Add GitHub Actions to run pytest with a coverage floor (the repo has a GitHub remote and no CI).

# Detailed Refactoring Recommendations

### R1: Host allow-list and security headers (SEC1, SEC2)
- **Problem / root cause:** there's no `Host` validation or anti-framing header on a portal that can act as you.
- **Fix:** use Flask 3.1's built-in `TRUSTED_HOSTS` plus headers set in `after_request`.

```python
# fdm/config.py
LOCAL_HOSTS = ("127.0.0.1", "localhost", "[::1]")
    trusted_hosts: tuple[str, ...] = LOCAL_HOSTS  # any other Host gets HTTP 400, which stops DNS rebinding
...
            trusted_hosts=_csv("FDM_TRUSTED_HOSTS", _default_hosts(host)),

def _default_hosts(host: str) -> tuple[str, ...]:
    # A wildcard bind can't know which name you'll use, so 0.0.0.0 needs FDM_TRUSTED_HOSTS.
    return LOCAL_HOSTS if host in {*LOCAL_HOSTS, "::1", "0.0.0.0", "::"} else (*LOCAL_HOSTS, host)
```
```python
# fdm/web/__init__.py
CSP = ("default-src 'self'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; "
       "form-action 'self'; base-uri 'none'; object-src 'none'")

    app.config.update(
        SECRET_KEY=os.getenv("FDM_PORTAL_SECRET") or secrets.token_hex(32),
        TRUSTED_HOSTS=list(settings.trusted_hosts),
        SESSION_COOKIE_SAMESITE="Lax",  # keeps the CSRF session off cross-site POSTs and framed pages
    )

    @app.after_request
    def security_headers(response):
        # Another site must not frame the bid form and click it through.
        response.headers.setdefault("Content-Security-Policy", CSP)
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        return response
```
- **Benefit:** closes the remote-bid chain. The CSP also blocks `javascript:` URLs, which backs up R2.
- **Risks:** anyone on `0.0.0.0` must set `FDM_TRUSTED_HOSTS`. `'unsafe-inline'` for styles is needed for the score
  bar's inline `style` ([index.html:59](fdm/web/templates/index.html#L59)).
- **Validation:** tests for unknown Host → 400, `127.0.0.1:5000` → 200, and the headers being present.

### R2: Accept only web links from feeds (SEC3)
```python
# fdm/sources.py
def _web_url(link: str) -> str:
    """The link if it is http(s), else "" so the gig is dropped: a feed must not put javascript: links in the portal."""
    link = link.strip()
    return link if urlparse(link).scheme.lower() in {"http", "https"} else ""

        link = _web_url(item.findtext("link") or "")                                        # RSS
        link = _web_url((link_el.get("href") if link_el is not None else "") or "")         # Atom
```
The existing `if gig.title and gig.url` filter then drops the gig. **Risk:** feeds that use relative links get
dropped, which is rare. **Validation:** a `javascript:` RSS item yields `[]`.

### R3: Atomic writes and strict bid-log reads (DB1, DB2)
```python
# fdm/store.py
def write_text(path: Path, text: str) -> None:
    """Replace the file in one step: after a crash, readers find the old file or the new one, never half."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in range(5):  # Windows briefly refuses to replace a file a reader has open
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.05)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise

def write_json(path: Path, data: object) -> None:
    write_text(path, json.dumps(data, indent=2, default=str))
```
```python
# fdm/bids.py
class BidLogError(BidError):
    """A bid log exists but can't be read, so whether a bid was already placed is unknown."""

def placed_bids(output_dir: Path, demo_id: str, *, strict: bool = False) -> list[dict]:
    path = output_dir / BID_DIR / f"{demo_id}.json"
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        log.error("Cannot read the bid log %s: %s", path, exc)
        if strict:  # never bid, or rewrite the log, on the guess that there was no bid
            raise BidLogError(f"{path} cannot be read, so the portal can't tell whether a bid was already "
                              "placed. Fix or move that file, then try again.") from exc
        return []
    ...

def save_bid(output_dir: Path, demo_id: str, record: dict) -> None:
    write_json(output_dir / BID_DIR / f"{demo_id}.json", [*placed_bids(output_dir, demo_id, strict=True), record])
```
In the `bid` route, call `placed_bids(..., strict=True)` inside the lock and turn a `BidError` into the form error.
Wrap `save_bid` so that on failure it logs the full record with `log.exception` and flashes "placed on Freelancer.com
but not recorded" instead of returning a 500. **Risk:** none to the file format. **Validation:** a corrupt log blocks
bidding and leaves the file untouched, and the existing round-trip tests still pass.

### R4: Validate settings (CQ1)
```python
# fdm/config.py
class ConfigError(ValueError):
    """A setting is unusable. The message names the variable."""

def _int(name: str, default: int, minimum: int = 0, maximum: int | None = None) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name}={raw!r} is not a whole number") from None
    if value < minimum or (maximum is not None and value > maximum):
        bounds = f"from {minimum} to {maximum}" if maximum is not None else f"at least {minimum}"
        raise ConfigError(f"{name} must be {bounds}, not {value}")
    return value
# llm_concurrency=_int("FDM_LLM_CONCURRENCY", 2, 1); port=_int("FDM_PORT", 5000, 1, 65535); shortlist_size min 1
# log_level must be one of DEBUG/INFO/WARNING/ERROR; secret fields get field(repr=False)
```
`server.main` catches `ConfigError`, prints it, and returns 2. **Risk:** bad values that used to be tolerated now stop
startup, which is the point.

### R5: One deadline per proposal (PERF3)
```python
# fdm/llm.py: complete() keeps its contract; the old loop body moves to _try_chain unchanged
        async with self._semaphore:  # the deadline starts after queueing, so waiting gigs aren't penalised
            try:
                async with asyncio.timeout(self._deadline):
                    return await self._try_chain(messages, parse, json_schema, temperature, failures)
            except TimeoutError:
                tried = "; ".join(failures[-3:]) or "no reply yet"
                raise LLMError(f"no usable reply within {self._deadline:.0f}s: {tried}") from None
```
The deadline comes from `FDM_PROPOSAL_TIMEOUT`, default 600. Also move the `status >= 500 or 408` check above the
limit hints (CQ3). **Validation:** a MockTransport that sleeps past a 0.1 s deadline raises `LLMError`. The existing
9 LLM tests stay green.

### R6: Fetch gigs with httpx instead of Playwright (PERF2, DEP2)
```python
# fdm/sources.py
async def fetch_freelancer(client: httpx.AsyncClient, query: str, limit: int) -> list[Gig]:
    params = [("limit", limit), ("full_description", "true"), ("job_details", "true"), ("languages[]", "en"),
              *(("jobs[]", skill) for skill in FREELANCER_SKILLS)]
    if query:
        params.append(("query", query))
    response = await client.get(FREELANCER_API, params=params)
    if not response.is_success:
        raise RuntimeError(f"Freelancer.com returned HTTP {response.status_code} for query {query or '<latest>'!r}")
    return parse_freelancer_projects(response.json())

async def fetch_feed(client: httpx.AsyncClient, url: str) -> list[Gig]:
    response = await client.get(url)
    if not response.is_success:
        raise RuntimeError(f"feed {url} returned HTTP {response.status_code}")
    return parse_feed(response.text, url)

async def fetch_gigs(settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> list[Gig]:
    """Query every configured source concurrently and de-duplicate the results."""
    async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT}, timeout=30, follow_redirects=True,
                                 transport=transport) as client:
        jobs = [fetch_freelancer(client, q, settings.results_per_query) for q in ("", *settings.search_queries)]
        jobs += [fetch_feed(client, url) for url in settings.rss_feeds]
        batches = await asyncio.gather(*jobs, return_exceptions=True)
    ...  # de-duplication unchanged
```
- **Behaviour kept:** the same params (repeated `jobs[]`), the 30 s timeout, redirects (Playwright follows them by
  default), concurrency and de-duplication.
- **Benefit:** 113 MB less to install, no Node subprocess, and the fetch path becomes testable with `MockTransport`.
- **Risk:** the User-Agent text changes to `Shortlister/0.1 (+python httpx)`. Update the README "How it works" table
  too.

### R7: Find the latest proposals in one scan (PERF1)
```python
# fdm/versions.py: demo ids end in a gig-id suffix that never contains "v", so "-vN" is unambiguous
_FILE = re.compile(r"(?P<demo_id>[a-z0-9-]+?)(?:-v(?P<number>\d+))?\.txt")

def latest_proposals(output_dir: Path, demo_ids: Iterable[str]) -> dict[str, Version]:
    """The newest proposal of each of these gigs, from a single scan of the folder."""
    wanted, newest = set(demo_ids), {}
    try:
        entries = list(os.scandir(output_dir / PROPOSAL_DIR))
    except OSError:
        return {}
    for entry in entries:
        match = _FILE.fullmatch(entry.name)
        if match and match["demo_id"] in wanted and int(match["number"] or 1) > newest.get(match["demo_id"], (0,))[0]:
            newest[match["demo_id"]] = (int(match["number"] or 1), entry)
    return {demo_id: Version(n, Path(e.path), datetime.fromtimestamp(e.stat().st_mtime), _read_meta(Path(e.path)))
            for demo_id, (n, e) in newest.items()}
```
The index then makes one call instead of 10 globs, and reads 10 sidecars instead of every version's sidecar.
**Validation:** the prefix-collision test plus a new test with `-v10` vs `-v9`.

### R8: Formatting and view-model module (AR2, AR3, CQ2)
Move `bid_label` from `web/jobs.py`, and `money`/`bid_amount` from `bids.py`, into `fdm/web/format.py`. Add a
`Suggestion.of(meta)` that returns the bid label and delivery days, used by all 4 call sites. Have `web.refresh` call
`render_table` and pass the table to `store`, so the store no longer depends on `ranking`. Route bodies shrink and
nothing visible changes. **Validation:** the existing web tests assert the exact HTML strings.

### R9 (needs a decision): save a gig snapshot with each proposal (AR4)
Add `"gig": asdict(gig)` to the sidecar. `shortlisted()` would then fall back to the latest sidecar's gig, so "Review &
place bid" and "New proposal" keep working after a refresh drops the gig. This is backward-compatible: old sidecars
just don't have the snapshot.

### R10 (needs a decision): replace freelancersdk with a small httpx client (DEP1)
It would be a `Freelancer` class covering `GET /api/users/0.1/self/`, `GET /api/projects/0.1/projects/{id}/` and
`POST /api/projects/0.1/bids/`. It sends the same `Freelancer-OAuth-V1` header, uses a native timeout, and keeps the
same `BidError` messages naming the failed step. The bid record format stays the same. It removes 66 MB and the
timeout patch.

# Test Improvements

- **Security regressions:** unknown Host → 400; framing and CSP headers present; `javascript:` feed item dropped;
  corrupt bid log blocks bidding and is left untouched; "placed but not recorded" shows a message instead of a 500.
- **Untested paths:**
  - Fetching in [sources.py](fdm/sources.py) is at 63% coverage. After R6, add MockTransport tests for
    de-duplication, partial source failure and all sources failing.
  - The `/refresh` route (lines 106–118 of [web/\_\_init\_\_.py](fdm/web/__init__.py#L106-L118)).
  - The JobManager crash path (lines 115–121 of [jobs.py](fdm/web/jobs.py#L115-L121)).
- **Settings:** a table of invalid values, each giving a `ConfigError` that names the variable.
- **Concurrency:** two threaded `POST` bids result in exactly one `place_bid` call.
- **LLM:** the deadline, and the 5xx-vs-limit classification.
- **Performance budget (optional):** index under 50 ms with 5k proposal files.
- **CI:** GitHub Actions on Python 3.11 and 3.12, running `pytest` with `coverage --fail-under=88`.

# Migration Notes

- **`FDM_HOST=0.0.0.0` users** must set `FDM_TRUSTED_HOSTS=<LAN IP or hostname>`, or they'll get HTTP 400.
- **`output/` stays fully compatible.** Atomic writes produce the same bytes, new sidecar keys are optional, and
  pre-versioning files still count as v1.
- **Bad settings that used to be ignored** now stop startup with a clear message.
- **Playwright and the SDK:** after R6 and R10, `pip uninstall playwright freelancersdk guzzle_sphinx_theme sphinx` is
  optional cleanup.
- **CSP:** any future inline `<script>` will need a nonce.
- **Rollout:** one commit per batch, tests green each time. Each batch reverts on its own.

# Expected Performance Gains

| Area | Today | After | Basis |
|---|---|---|---|
| `GET /` with 6,060 proposal files | 147 ms | about 25 ms, flat as history grows | Measured today; projected after R7 |
| HTTP client start per refresh | 739 ms | 536 ms | Measured, median of 5 |
| Full refresh | about 15 s | about 15 s | Limited by network time; no real change |
| Install size | 232 MB in site-packages | −113 MB with R6; −66 MB more with R10 | Measured |
| Stalled proposal | up to about 60 min | at most the deadline (default 10 min) | R5 |
| Job history memory | unbounded | capped at 50 jobs | Quick win |

# Final Target Architecture

Same flat package, sharper edges. No new layers or frameworks: Pydantic, FastAPI and SPA rewrites would add cost
without a benefit for a single-user local tool.

```
fdm/
  config.py      Settings: validated, secrets hidden from repr, trusted_hosts
  models.py      Gig, ScoredGig, Pitch, Proposal, ProposalMeta, BidRecord (typed, same JSON)
  sources.py     httpx: Freelancer API + RSS/Atom (http(s) links only)
  ranking.py     pure scoring + render_table
  llm.py         OllamaCloud (one deadline per proposal, corrected error classification)
  agents.py      Pitch Writer prompt + parsing
  generator.py   generate → save version (+ gig snapshot, if approved)
  store.py       atomic write_text/write_json, shortlist I/O
  versions.py    versions + latest_proposals() single scan
  bids.py        bid rules + strict bid log  (+ freelancer.py httpx client, if approved)
  web/__init__.py  create_app: config, trusted hosts, headers, CSRF, routes kept thin
  web/format.py    bid_label, money, Suggestion, row builders
  web/jobs.py      JobManager (history capped)
```

# Open Decisions

1. **Batch 1** (R1–R4 plus their tests) is ready to implement once approved.
2. **R10:** replace freelancersdk with direct httpx calls
3. **R9:** keep gigs biddable after they drop off the shortlist?
