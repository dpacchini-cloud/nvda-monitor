# NVDA Exit-Multiple Regime Monitor

Signal-weighted dashboard that maps live data-center and GPU-rental metrics to an implied NVDA P/E regime.

## Files

- `nvda-regime-monitor.html` — the dashboard (self-contained, no build step)
- `fetch_signals.py` — pulls free public data, writes `signals.json`
- `.github/workflows/refresh.yml` — runs the fetcher daily at 12:17 UTC
- `signals.json` — created and updated automatically by the Action
- `NVDA_Multiple_Compression_Model.xlsx` — the underlying scenario model

## Deploy in 5 minutes

1. Create a new GitHub repo, upload these files (keeping the `.github/workflows/` folder).
2. In the repo: **Settings → Pages → Source: Deploy from a branch → main / root**. Your dashboard is live at `https://<you>.github.io/<repo>/nvda-regime-monitor.html`.
3. In **Actions**, run the **Refresh signals** workflow manually once to seed `signals.json`. From then on it runs daily; the page loads the latest JSON on each visit.
4. Optional: customize the `UA` string in `fetch_signals.py` with your email (SEC EDGAR requires it).

## What's live vs. manual

| Signal | Weight | Source | Cadence |
|---|---|---|---|
| NVDA share price | — | Stooq CSV | Daily |
| NVDA revenue QoQ (DC proxy) | 20% | SEC EDGAR XBRL | Quarterly, ~30d after Q close |
| Hyperscaler capex YoY blend | 20% | SEC EDGAR (MSFT/GOOGL/META/AMZN) | Quarterly |
| H100 1-yr contract rate | 25% | **Manual** — SemiAnalysis subscription | Daily |
| EPS revisions | 15% | **Manual** — stockanalysis / ChartMill | Weekly |
| Networking rev growth | 10% | **Manual** — NVDA earnings deck | Quarterly |
| ASIC share of hyperscaler build | 10% | **Manual** — SemiAnalysis / commentary | Quarterly |

Live signals show a teal left border and a "as of DATE / source" chip above the dropdown. Manual signals show an amber border. Overrides made via the dropdowns are saved to the URL hash so a bookmarked link preserves state and survives refreshes.

## Adding paywalled feeds

If you subscribe to Silicon Data or SemiAnalysis and want the H100 rental rate to auto-populate, add a fetcher function to `fetch_signals.py`:

```python
def h100_1yr_rate():
    key = os.environ['SEMIANALYSIS_KEY']  # set as GH Actions secret
    ...
    return {'value': rate_dollar_hr, 'trend_3mo_pct': trend, 'as_of': date, 'source': 'SemiAnalysis'}
```

Then in the HTML, wire the `h100` signal's `auto` block to read `LIVE.h100_1yr.trend_3mo_pct` and map to a score.

## Threshold logic

The auto-mapped score for each signal (−2 to +2) uses these bands (see `fetch_signals.py` outputs and the `map` functions in the HTML):

- **NVDA revenue QoQ:** <0 → −2, 0–5% → −1, 5–12% → 0, 12–20% → +1, >20% → +2
- **Hyperscaler capex YoY blend:** <−5% → −2, −5 to 0 → −1, 0–10% → 0, 10–25% → +1, >25% → +2

Composite score maps linearly to a P/E: score of 0 → 20.5x; each 1.0 in score shifts the multiple by 3.25x.

## Not investment advice

Scenario tool for personal use. All signal thresholds and weights are opinionated defaults — tune to your own thesis.
