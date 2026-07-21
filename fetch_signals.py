#!/usr/bin/env python3
"""Fetch live signals for the NVDA regime monitor.

Runs on a GitHub Actions schedule and writes signals.json to the repo root.
Every signal is wrapped in try/except so one failure never kills the run.

IMPORTANT: set CONTACT_EMAIL below to a REAL email. SEC EDGAR's fair-access
policy rejects requests whose User-Agent looks generic or fake (403 Forbidden).
"""
import json, sys, time, gzip, io, datetime
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

# ---- EDIT THIS ----
CONTACT_EMAIL = 'dpacchini@gmail.com'   # <-- put your real email here
GH_HANDLE = 'dpacchini-cloud'
# SEC wants: "Company/Project Name AdminContact@domain.com"
SEC_UA = f'nvda-monitor/{GH_HANDLE} {CONTACT_EMAIL}'
TIMEOUT = 30
RETRIES = 3
BACKOFF = 2.0  # seconds, doubles each retry


def http_get(url, ua=SEC_UA, extra=None, retries=RETRIES):
    """GET with retry/backoff and transparent gzip. Raises on final failure."""
    hdr = {
        'User-Agent': ua,
        'Accept-Encoding': 'gzip, deflate',
        'Accept': 'application/json, text/csv, */*',
    }
    if extra:
        hdr.update(extra)
    last = None
    for attempt in range(retries):
        try:
            with urlopen(Request(url, headers=hdr), timeout=TIMEOUT) as r:
                raw = r.read()
                if r.headers.get('Content-Encoding') == 'gzip':
                    raw = gzip.decompress(raw)
                return raw
        except HTTPError as e:
            last = e
            # 403/429 from SEC are often transient rate-limits on shared IPs; back off
            if e.code in (403, 429, 500, 502, 503) and attempt < retries - 1:
                time.sleep(BACKOFF * (2 ** attempt))
                continue
            raise
        except URLError as e:
            last = e
            if attempt < retries - 1:
                time.sleep(BACKOFF * (2 ** attempt))
                continue
            raise
    if last:
        raise last


# ---------- NVDA share price (fallback chain) ----------
def nvda_price():
    errors = []
    # Source 1: Stooq daily CSV (l/ endpoint)
    for url in (
        'https://stooq.com/q/l/?s=nvda.us&f=sd2t2ohlcv&h&e=csv',
        'https://stooq.pl/q/l/?s=nvda.us&f=sd2t2ohlcv&h&e=csv',
    ):
        try:
            raw = http_get(url, ua='Mozilla/5.0 (nvda-monitor)').decode('utf-8', 'replace')
            lines = raw.strip().split('\n')
            if len(lines) >= 2 and 'N/D' not in lines[1]:
                row = lines[1].split(',')
                close = float(row[6])
                if close > 0:
                    return {'value': round(close, 2), 'as_of': row[1],
                            'source': 'Stooq', 'url': 'https://stooq.com/q/?s=nvda.us'}
            errors.append(f'stooq: no data ({lines[1][:40] if len(lines)>1 else "empty"})')
        except Exception as e:
            errors.append(f'stooq: {type(e).__name__}: {e}')
    # Source 2: Stooq historical daily CSV (d/ endpoint) — different path, often up when l/ is 404
    try:
        raw = http_get('https://stooq.com/q/d/l/?s=nvda.us&i=d',
                       ua='Mozilla/5.0 (nvda-monitor)').decode('utf-8', 'replace')
        lines = [l for l in raw.strip().split('\n') if l and l[0].isdigit()]
        if lines:
            last = lines[-1].split(',')  # Date,Open,High,Low,Close,Volume
            close = float(last[4])
            return {'value': round(close, 2), 'as_of': last[0],
                    'source': 'Stooq (daily history)', 'url': 'https://stooq.com/q/d/?s=nvda.us'}
    except Exception as e:
        errors.append(f'stooq-hist: {type(e).__name__}: {e}')
    return {'error': ' | '.join(errors)}


# ---------- SEC EDGAR helpers ----------
def sec_facts(cik):
    url = f'https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json'
    return json.loads(http_get(url))


def quarterly_series(facts, concepts):
    for concept in concepts:
        try:
            raw = facts['facts']['us-gaap'][concept]['units']['USD']
        except KeyError:
            continue
        rows, seen = [], set()
        for x in raw:
            if x.get('form') not in ('10-Q', '10-K'):
                continue
            if 'start' not in x or 'end' not in x:
                continue
            try:
                s = datetime.date.fromisoformat(x['start'])
                e = datetime.date.fromisoformat(x['end'])
            except ValueError:
                continue
            if not (60 < (e - s).days < 100):
                continue
            if x['end'] in seen:
                continue
            seen.add(x['end'])
            rows.append({'end': x['end'], 'val': x['val']})
        if len(rows) >= 5:
            return sorted(rows, key=lambda r: r['end']), concept
    return [], None


def nvda_revenue_trend():
    try:
        facts = sec_facts(1045810)
        series, concept = quarterly_series(facts, [
            'RevenueFromContractWithCustomerExcludingAssessedTax',
            'Revenues',
        ])
        if len(series) < 5:
            return {'error': 'insufficient quarterly points from EDGAR'}
        latest, prior_q, year_ago = series[-1], series[-2], series[-5]
        return {
            'qoq_pct': round((latest['val'] / prior_q['val'] - 1) * 100, 1),
            'yoy_pct': round((latest['val'] / year_ago['val'] - 1) * 100, 1),
            'latest_bn': round(latest['val'] / 1e9, 2),
            'as_of': latest['end'],
            'source': f'SEC EDGAR ({concept})',
            'url': 'https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=0001045810&type=10-Q',
            'note': 'Total revenue proxy — DC segment isn\'t XBRL-tagged; DC ~90%+ of revenue since FY26.',
        }
    except Exception as e:
        return {'error': f'{type(e).__name__}: {e}'}


def hyperscaler_capex():
    ciks = {'MSFT': 789019, 'GOOGL': 1652044, 'META': 1326801, 'AMZN': 1018724}
    components = {}
    for name, cik in ciks.items():
        try:
            facts = sec_facts(cik)
            series, _ = quarterly_series(facts, [
                'PaymentsToAcquirePropertyPlantAndEquipment',
                'PaymentsToAcquireProductiveAssets',
            ])
            if len(series) < 5:
                components[name] = {'error': 'insufficient data'}
                continue
            latest, year_ago = series[-1], series[-5]
            components[name] = {
                'latest_bn': round(latest['val'] / 1e9, 2),
                'yoy_pct': round((latest['val'] / year_ago['val'] - 1) * 100, 1),
                'as_of': latest['end'],
            }
            time.sleep(0.3)  # be polite to SEC between companies
        except Exception as e:
            components[name] = {'error': f'{type(e).__name__}: {e}'}
    valid = [v for v in components.values() if 'yoy_pct' in v]
    if not valid:
        return {'error': 'no components succeeded', 'components': components}
    return {
        'yoy_pct': round(sum(v['yoy_pct'] for v in valid) / len(valid), 1),
        'components': components,
        'as_of': max(v['as_of'] for v in valid),
        'source': 'SEC EDGAR — PaymentsToAcquirePropertyPlantAndEquipment, MSFT/GOOGL/META/AMZN blend',
        'url': 'https://www.sec.gov/edgar/searchedgar/companysearch',
    }


signals = {
    'updated_at': datetime.datetime.utcnow().replace(microsecond=0).isoformat() + 'Z',
    'contact_configured': CONTACT_EMAIL != 'YOUR_REAL_EMAIL@example.com',
    'price': nvda_price(),
    'nvda_revenue': nvda_revenue_trend(),
    'hyperscaler_capex': hyperscaler_capex(),
    'manual': {
        'note': 'H100 rental and ASIC share are behind subscription paywalls (SemiAnalysis, Silicon Data). '
                'Update these via the URL hash on the dashboard; values persist in the shared link.'
    },
}

json.dump(signals, sys.stdout, indent=2)
sys.stdout.write('\n')
