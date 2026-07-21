#!/usr/bin/env python3
"""Fetch live signals for the NVDA regime monitor.

Runs on a GitHub Actions schedule and writes signals.json to the repo root.
Every signal is wrapped in try/except so one failure never kills the run.

IMPORTANT: set CONTACT_EMAIL below to a REAL email. SEC EDGAR's fair-access
policy rejects requests whose User-Agent looks generic or fake (403 Forbidden).
"""
import json, sys, time, gzip, datetime
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

# ---- EDIT THIS ----
CONTACT_EMAIL = 'dpacchini@gmail.com'   # <-- put your real email here
GH_HANDLE = 'dpacchini-cloud'
SEC_UA = f'nvda-monitor/{GH_HANDLE} {CONTACT_EMAIL}'
TIMEOUT = 30
RETRIES = 3
BACKOFF = 2.0


def http_get(url, ua=SEC_UA, extra=None, retries=RETRIES):
    hdr = {'User-Agent': ua, 'Accept-Encoding': 'gzip, deflate',
           'Accept': 'application/json, text/csv, */*'}
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
        except (HTTPError, URLError) as e:
            last = e
            code = getattr(e, 'code', None)
            if (code in (403, 429, 500, 502, 503) or code is None) and attempt < retries - 1:
                time.sleep(BACKOFF * (2 ** attempt))
                continue
            raise
    if last:
        raise last


# ---------- NVDA share price (multi-source fallback) ----------
def _from_stooq():
    raw = http_get('https://stooq.com/q/l/?s=nvda.us&f=sd2t2ohlcv&h&e=csv',
                   ua='Mozilla/5.0 (nvda-monitor)').decode('utf-8', 'replace')
    lines = raw.strip().split('\n')
    if len(lines) < 2 or 'N/D' in lines[1]:
        raise ValueError('stooq no data')
    row = lines[1].split(',')
    close = float(row[6])
    if close <= 0:
        raise ValueError('stooq zero')
    return {'value': round(close, 2), 'as_of': row[1], 'source': 'Stooq',
            'url': 'https://stooq.com/q/?s=nvda.us'}


def _from_yahoo():
    url = ('https://query1.finance.yahoo.com/v8/finance/chart/NVDA'
           '?range=5d&interval=1d')
    raw = http_get(url, ua='Mozilla/5.0 (nvda-monitor)').decode('utf-8', 'replace')
    j = json.loads(raw)
    res = j['chart']['result'][0]
    meta = res.get('meta', {})
    price = meta.get('regularMarketPrice')
    if price and price > 0:
        ts = meta.get('regularMarketTime')
        as_of = (datetime.datetime.utcfromtimestamp(ts).date().isoformat()
                 if ts else datetime.date.today().isoformat())
        return {'value': round(float(price), 2), 'as_of': as_of, 'source': 'Yahoo Finance',
                'url': 'https://finance.yahoo.com/quote/NVDA'}
    closes = res['indicators']['quote'][0]['close']
    stamps = res['timestamp']
    for c, t in zip(reversed(closes), reversed(stamps)):
        if c:
            return {'value': round(float(c), 2),
                    'as_of': datetime.datetime.utcfromtimestamp(t).date().isoformat(),
                    'source': 'Yahoo Finance (close)', 'url': 'https://finance.yahoo.com/quote/NVDA'}
    raise ValueError('yahoo no price')


def nvda_price():
    errors = []
    for fn in (_from_yahoo, _from_stooq):
        try:
            return fn()
        except Exception as e:
            errors.append(f'{fn.__name__}: {type(e).__name__}: {e}')
    return {'error': ' | '.join(errors)}


# ---------- SEC EDGAR ----------
def sec_facts(cik):
    return json.loads(http_get(f'https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json'))


def latest_quarters(facts, concepts, n=12):
    """Merge all candidate concepts, keep the latest-FILED value per quarter-end,
    return the last `n` genuine quarterly points sorted oldest->newest."""
    by_end = {}
    for concept in concepts:
        try:
            rows = facts['facts']['us-gaap'][concept]['units']['USD']
        except KeyError:
            continue
        for x in rows:
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
            filed = x.get('filed', '')
            prev = by_end.get(x['end'])
            if prev is None or filed > prev[0]:
                by_end[x['end']] = (filed, x['val'])
    pts = sorted(({'end': k, 'val': v[1]} for k, v in by_end.items()),
                 key=lambda r: r['end'])
    return pts[-n:] if len(pts) >= n else pts


def nvda_revenue_trend():
    try:
        facts = sec_facts(1045810)
        series = latest_quarters(facts, [
            'RevenueFromContractWithCustomerExcludingAssessedTax', 'Revenues'])
        if len(series) < 5:
            return {'error': 'insufficient quarterly points'}
        latest, prior_q = series[-1], series[-2]
        if latest['val'] < 5e9:
            return {'error': f'suspect stale value {latest["val"]/1e9:.1f}bn as of {latest["end"]}'}
        d1 = datetime.date.fromisoformat(latest['end'])
        year_ago, best = None, None
        for pt in series[:-1]:
            d0 = datetime.date.fromisoformat(pt['end'])
            gap = abs((d1 - d0).days - 365)
            if best is None or gap < best:
                best, year_ago = gap, pt
        return {
            'qoq_pct': round((latest['val'] / prior_q['val'] - 1) * 100, 1),
            'yoy_pct': round((latest['val'] / year_ago['val'] - 1) * 100, 1),
            'latest_bn': round(latest['val'] / 1e9, 2),
            'as_of': latest['end'],
            'source': 'SEC EDGAR (total revenue)',
            'url': 'https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=0001045810&type=10-Q',
            'note': 'Total revenue proxy — DC segment isn\'t XBRL-tagged; DC ~90%+ since FY26.',
        }
    except Exception as e:
        return {'error': f'{type(e).__name__}: {e}'}


def hyperscaler_capex():
    ciks = {'MSFT': 789019, 'GOOGL': 1652044, 'META': 1326801, 'AMZN': 1018724}
    comp = {}
    for name, cik in ciks.items():
        try:
            facts = sec_facts(cik)
            series = latest_quarters(facts, [
                'PaymentsToAcquirePropertyPlantAndEquipment',
                'PaymentsToAcquireProductiveAssets'])
            if len(series) < 5:
                comp[name] = {'error': 'insufficient data'}
                continue
            latest = series[-1]
            d1 = datetime.date.fromisoformat(latest['end'])
            # find the quarter closest to 365 days before the latest (handles gaps in the series)
            year_ago, best = None, None
            for pt in series[:-1]:
                d0 = datetime.date.fromisoformat(pt['end'])
                gap = abs((d1 - d0).days - 365)
                if best is None or gap < best:
                    best, year_ago = gap, pt
            d0 = datetime.date.fromisoformat(year_ago['end'])
            if not (330 < (d1 - d0).days < 400):
                comp[name] = {'error': f'no ~1yr baseline (nearest {year_ago["end"]}->{latest["end"]})'}
                continue
            comp[name] = {'latest_bn': round(latest['val'] / 1e9, 2),
                          'yoy_pct': round((latest['val'] / year_ago['val'] - 1) * 100, 1),
                          'as_of': latest['end']}
            time.sleep(0.3)
        except Exception as e:
            comp[name] = {'error': f'{type(e).__name__}: {e}'}
    valid = [v for v in comp.values() if 'yoy_pct' in v]
    if not valid:
        return {'error': 'no components succeeded', 'components': comp}
    return {'yoy_pct': round(sum(v['yoy_pct'] for v in valid) / len(valid), 1),
            'components': comp,
            'as_of': max(v['as_of'] for v in valid),
            'source': 'SEC EDGAR — capex (PP&E), MSFT/GOOGL/META/AMZN blend',
            'url': 'https://www.sec.gov/edgar/searchedgar/companysearch'}


signals = {
    'updated_at': datetime.datetime.utcnow().replace(microsecond=0).isoformat() + 'Z',
    'contact_configured': CONTACT_EMAIL != 'YOUR_REAL_EMAIL@example.com',
    'price': nvda_price(),
    'nvda_revenue': nvda_revenue_trend(),
    'hyperscaler_capex': hyperscaler_capex(),
    'manual': {'note': 'H100 rental and ASIC share are behind subscription paywalls '
                       '(SemiAnalysis, Silicon Data). Update via the URL hash on the dashboard.'},
}

json.dump(signals, sys.stdout, indent=2)
sys.stdout.write('\n')
