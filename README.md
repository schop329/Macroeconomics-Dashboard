CPI / PPI Monthly Intelligence Dashboard
A static dashboard that reads a single `data.json` file. That file is rebuilt
on a schedule by a GitHub Action that pulls CPI & PPI from BLS and the Fed
Funds rate from FRED, server-side. The browser never calls BLS or FRED, so
there is no CORS dependency and no API key in anyone's browser.
```
your-repo/
├── index.html              ← the dashboard (reads data.json)
├── data.json               ← generated + committed by the Action
├── scripts/
│   ├── fetch_data.py        ← the data puller (standard library only)
│   └── generate_commentary.py  ← optional AI analysis blurbs (failure-isolated)
└── .github/workflows/
    └── refresh-data.yml     ← scheduled refresh
```
Why it was rebuilt this way
BLS and FRED both block direct browser calls (CORS). The previous version worked
around that by routing fetches through an AI tool and, on failure, falling back to
AI-recalled numbers — fragile, and it could show stale/estimated figures. This
version moves the fetch off the browser entirely: a scheduled job pulls the
data with your own keys and writes a plain JSON file the page reads same-origin.
Because CPI/PPI/Fed-Funds publish only monthly, a scheduled build is the right fit
and is the most reliable, lowest-error option for a shared team dashboard.
If a pull fails, the dashboard shows `Data Pull Failed (E###)` with a numbered
code — never estimated or fabricated data.
One-time setup
Create the two free API keys
BLS registration key: https://data.bls.gov/registrationEngine/
FRED API key: https://fredaccount.stlouisfed.org/apikeys (optional — only
powers the Fed Funds chart; CPI/PPI work without it)
Add them as repository secrets
Repo → Settings → Secrets and variables → Actions → New repository secret
`BLS_API_KEY` → your BLS key
`FRED_API_KEY` → your FRED key (optional)
Put the files in place exactly as shown in the tree above
(`fetch_data.py` inside `scripts/`, the workflow inside `.github/workflows/`).
Generate the first `data.json`
Go to the Actions tab → Refresh dashboard data → Run workflow.
It pulls the data and commits `data.json` to the repo root.
Turn on GitHub Pages
Repo → Settings → Pages → Source: Deploy from a branch → Branch: `main`,
folder `/ (root)` → Save. Your team opens the Pages URL it gives you.
That's it. After this, the Action refreshes the data on its schedule and the page
stays current — anyone who opens the URL (or clicks Refresh Data) sees the
latest committed figures. No keys, no proxy, no setup on their end.
Running the puller locally (optional)
```bash
export BLS_API_KEY=xxxxxxxx
export FRED_API_KEY=xxxxxxxx        # optional
python3 scripts/fetch_data.py       # writes ./data.json
python3 -m http.server 8000         # then open http://localhost:8000
```
The exit code is `0` on success or the numbered error code on failure (below).
Error codes
Shown on the dashboard as `Data Pull Failed (E###)` and printed by the puller.
Code	Where	Meaning
101	config	`BLS_API_KEY` secret/env var is not set
201	BLS · CPI	CPI request failed (network / HTTP / timeout)
202	BLS · CPI	BLS returned a non-success status (e.g. daily threshold)
203	BLS · CPI	Response had no `Results.series`
204	BLS · CPI	No usable headline series (`CUSR0000SA0`) returned
301	BLS · PPI	PPI request failed (network / HTTP / timeout)
302	BLS · PPI	BLS returned a non-success status
303	BLS · PPI	Response had no `Results.series`
304	BLS · PPI	No usable headline series (`WPSFD4`) returned
102	FRED	`FRED_API_KEY` missing — non-fatal, Fed chart only
401	FRED	FRED request failed — non-fatal, Fed chart only
402	FRED	FRED returned no observations — non-fatal
501	write	Could not write `data.json`
502	server	Unexpected exception during the pull
601	browser	Page could not download `data.json` (network / 404)
602	browser	`data.json` downloaded but is not valid JSON
603	browser	Server pull reported failure (see its own E### too)
604	browser	`data.json` valid but contains no CPI headline series
699	browser	Unexpected error while loading
`1xx` config · `2xx` BLS CPI · `3xx` BLS PPI · `4xx` FRED (non-fatal) ·
`5xx` server write/unknown · `6xx` browser-side load.
FRED is optional: a `102/401/402` leaves CPI & PPI fully working and just hides the
Fed Funds chart with a small note. A non-fatal FRED code is recorded in
`data.json` under `status.fred_error_code`.
How "failed but still usable" works
On a failed scheduled run the puller keeps the last good `cpi`/`ppi`/`fed_funds`
payload and flips `status.ok` to `false` with the error code. The dashboard then
shows a prominent Data Pull Failed (E###) banner and keeps displaying the most
recent successful official figures (clearly labeled), so the team isn't staring
at a blank page over a transient BLS hiccup. If you'd rather it show only the error
and hide the old data, say so — it's a one-line change in `loadAllData()`.
Adjusting the schedule
Edit the `cron` in `.github/workflows/refresh-data.yml`. Times are UTC. The default
`0 14 * * 1-5` is weekday mornings ET. You can always trigger a run by hand from the
Actions tab.
AI commentary (enabled)
The dashboard's narrative slots are filled server-side by
`scripts/generate_commentary.py`, which runs as a step in the same Action after
the data pull. It reads the freshly written `data.json`, asks the Anthropic API to
write the short analysis blurbs (headline/core read, energy & goods, transport,
Fed posture, the consumer-price lens, the auto/tire channel, and the two PPI
takes), and bakes them into `data.json` under a `commentary` object keyed by slot
id (`hc2`, `mfg2`, `transp2`, `fed2`, `lens2`, `tire2`, `ppi2`, `ppilens2`). The
page renders them with no browser API key.
To turn it on, add one more repository secret:
`ANTHROPIC_API_KEY` → your Anthropic key (from https://console.anthropic.com/)
That's the only extra setup. The model defaults to `claude-sonnet-4-6`; override it
by setting a `COMMENTARY_MODEL` env in the workflow step if you want.
This step is failure-isolated by design: it only runs when the data pull
succeeded, every API call is wrapped (one slot failing never stops the others),
and it always exits 0 — so commentary problems can never turn the data pull red
or corrupt `data.json`. Issues surface as GitHub `::warning::` annotations and as a
`status.commentary_error_code` in `data.json`:
Code	Meaning
701	`ANTHROPIC_API_KEY` not set — commentary skipped (data unaffected)
702	Every commentary call failed (e.g. API outage) — data unaffected
703	`data.json` missing/invalid, or the data pull itself failed — skipped
If you leave `ANTHROPIC_API_KEY` unset, the dashboard simply runs as a clean
data-and-charts tool with the narrative slots hidden — exactly as it does today.
