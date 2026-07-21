#!/usr/bin/env python3
"""Fetch live signals for the NVDA regime monitor.

Runs on a GitHub Actions schedule and writes signals.json to the repo root.
Each signal is wrapped in try/except so one failure never kills the run.
"""
import json, sys, datetime, urllib.request
from urllib.request import Request, urlopen

UA = 'NVDA Regime Monitor (github.com/YOUR_HANDLE) contact@example.com'
TIMEOUT = 25


def http_get(url, headers=None):
    hdr = {'User-Agent': UA, 'Accept-Encoding': 'identity'}
    if headers: hdr.update(headers)
    with urlopen(Request(url, headers=hdr), timeout=TIMEOUT) as r:
        return r.read()


# ---------- NVDA share price ----------
def nvda_price():
    try:
        raw = http_get('https://stooq.com/q/l/?s=nvda.us&f=sd2t2ohlcv&h&e=csv').decode()
        row = raw.strip().split('\n')[1].split(',')
        return {
            'value': round(float(row[6]), 2),
            'as_of': row[1],
            'source': 'Stooq',
            'url': 'https://stooq.com/q/?s=nvda.us',
        }
    except Exception as e:
        return {'error': f'{type(e).__name__}: {e}'}


# ---------- SEC EDGAR helpers ----------
def sec_facts(cik):
    url = f'https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json'
    return json.loads(http_get(url))


def quarterly_series(facts, concepts):
    """Return list of quarterly discrete revenue points, sorted oldest -> newest."""
    for concept in concepts:
        try:
            raw = facts['facts']['us-gaap'][concept]['units']['USD']
        except KeyError:
            continue
        rows = []
        seen = set()
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
            days = (e - s).days
            if not (60 < days < 100):
                continue
            key = x['end']
            if key in seen:
                continue
            seen.add(key)
            rows.append({'end': x['end'], 'val': x['val']})
        if len(rows) >= 5:
            return sorted(rows, key=lambda r: r['end']), concept
    return [], None


# ---------- NVDA revenue trend (proxy for DC growth) ----------
def nvda_revenue_trend():
    try:
        facts = sec_facts(1045810)
        series, concept = quarterly_series(facts, [
            'RevenueFromContractWithCustomerExcludingAssessedTax',
            'Revenues',
        ])
        if not series or len(series) < 5:
            return {'error': 'not enough quarterly points from EDGAR'}
        latest, prior_q, year_ago = series[-1], series[-2], series[-5]
        qoq = latest['val'] / prior_q['val'] - 1
        yoy = latest['val'] / year_ago['val'] - 1
        return {
            'qoq_pct': round(qoq * 100, 1),
            'yoy_pct': round(yoy * 100, 1),
            'latest_bn': round(latest['val'] / 1e9, 2),
            'as_of': latest['end'],
            'source': f'SEC EDGAR ({concept})',
            'url': 'https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=0001045810&type=10-Q',
            'note': 'Total revenue proxy — DC segment isn\'t tagged in XBRL; DC has been ~90%+ of revenue since FY26.',
        }
    except Exception as e:
        return {'error': f'{type(e).__name__}: {e}'}


# ---------- Hyperscaler capex blended YoY ----------
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
            yoy = latest['val'] / year_ago['val'] - 1
            components[name] = {
                'latest_bn': round(latest['val'] / 1e9, 2),
                'yoy_pct': round(yoy * 100, 1),
                'as_of': latest['end'],
            }
        except Exception as e:
            components[name] = {'error': f'{type(e).__name__}: {e}'}
    valid = [v for v in components.values() if 'yoy_pct' in v]
    if not valid:
        return {'error': 'no components succeeded', 'components': components}
    avg = sum(v['yoy_pct'] for v in valid) / len(valid)
    latest_end = max(v['as_of'] for v in valid)
    return {
        'yoy_pct': round(avg, 1),
        'components': components,
        'as_of': latest_end,
        'source': 'SEC EDGAR — PaymentsToAcquirePropertyPlantAndEquipment across MSFT/GOOGL/META/AMZN',
        'url': 'https://www.sec.gov/edgar/searchedgar/companysearch',
    }


# ---------- Assemble ----------
signals = {
    'updated_at': datetime.datetime.utcnow().replace(microsecond=0).isoformat() + 'Z',
    'price': nvda_price(),
    'nvda_revenue': nvda_revenue_trend(),
    'hyperscaler_capex': hyperscaler_capex(),
    'manual': {
        'note': 'H100 rental and ASIC share are behind subscription paywalls (SemiAnalysis, Silicon Data). '
                'Update these via the URL hash on the dashboard; the values persist in the shared link.'
    },
}

json.dump(signals, sys.stdout, indent=2)
sys.stdout.write('\n')
