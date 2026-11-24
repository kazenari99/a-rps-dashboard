"""RPS (相对价格强度) model: percentile rank of N-day returns across the whole market."""

from __future__ import annotations

import numpy as np
import pandas as pd

# Two parameter sets, both shown on the page.
SETS = {
    'A': dict(short=50, long=120, short_min=90, long_min=85, label='RPS 50/120'),
    'B': dict(short=120, long=250, short_min=90, long_min=85, label='RPS 120/250'),
}
PERIODS = sorted({p for s in SETS.values() for p in (s['short'], s['long'])})
MIN_LISTED_DAYS = 365
PULLBACK = -0.05  # close at least 5% below the 10-session high counts as 回踩
MIN_FLOAT_CAP = 30e8  # default page filter, CNY


def is_st(name):
    name = str(name).upper()
    return 'ST' in name or '退' in name


def features(bars):
    """Per-stock features from forward-adjusted closes (`adj`), counted in the stock's own bars."""
    bars = bars.sort_values(['thscode', 'date'])
    k = bars.groupby('thscode').cumcount(ascending=False)  # 0 = latest bar
    g = bars.groupby('thscode')
    last = bars[k == 0].set_index('thscode')
    f = pd.DataFrame(index=last.index)
    f['date'] = last['date']
    f['close'] = last['close']
    f['adj'] = last['adj']
    f['volume'] = last['volume']
    f['bars'] = g.size()
    prev = bars[k == 1].set_index('thscode')['adj']
    f['chg'] = f['adj'] / prev - 1
    for n in PERIODS:
        base = bars[k == n].set_index('thscode')['adj']
        f[f'r{n}'] = f['adj'] / base - 1  # NaN when the stock has fewer than n+1 bars
    win20 = bars[k < 20].groupby('thscode')
    f['ma20'] = win20['adj'].mean().where(f['bars'] >= 20)
    f['amt20'] = win20['turnover'].mean()
    f['hi10'] = bars[k < 10].groupby('thscode')['adj'].max()
    f['hi250'] = bars[k < 250].groupby('thscode')['adj'].max()
    f['ma20d'] = f['adj'] / f['ma20'] - 1
    f['pull10'] = f['adj'] / f['hi10'] - 1
    f['hi250d'] = f['adj'] / f['hi250'] - 1
    return f


def eligible(f, names, target):
    """Traded on the target day, listed at least a year, not ST / delisting."""
    cutoff = (pd.Timestamp(target) - pd.Timedelta(days=MIN_LISTED_DAYS)).strftime('%Y-%m-%d')
    f = f.join(names, how='inner')
    reasons = pd.Series('', index=f.index)
    reasons[f['list_date'].isna() | (f['list_date'].astype(str) > cutoff)] = '上市不满一年'
    reasons[f['name'].map(is_st)] = 'ST/退市'
    reasons[(f['date'] != pd.Timestamp(target)) | ~(f['volume'] > 0)] = '停牌/无当日数据'
    return f[reasons == ''].copy(), reasons[reasons != ''].value_counts().to_dict()


def rank(f):
    """RPS_N = percentile of the N-day return among stocks that have N+1 bars (0–100)."""
    for n in PERIODS:
        r = f[f'r{n}']
        f[f'rps{n}'] = (r.rank(pct=True) * 100).round(2).where(r.notna())
    for key, s in SETS.items():
        f[f'in{key}'] = (f[f"rps{s['short']}"] > s['short_min']) & (f[f"rps{s['long']}"] > s['long_min'])
    return f


def state(row):
    if pd.isna(row['ma20d']):
        return ''
    if row['ma20d'] < 0:
        return '跌破MA20'
    if row['pull10'] <= PULLBACK:
        return '回踩中'
    return '高位强势'


def streaks(history, today, codes_by_set):
    """Consecutive snapshot days in each set (today included) and the previous snapshot."""
    days = sorted(d for d in history if d < today)
    prev = days[-1] if days else None
    out = {}
    for key, codes in codes_by_set.items():
        counts = {}
        for code in codes:
            n = 1
            for d in reversed(days):
                if code in set(history[d].get(key, [])):
                    n += 1
                else:
                    break
            counts[code] = n
        out[key] = counts
    return out, prev


def market_state(index_close, target, f):
    s = index_close[index_close.index <= pd.Timestamp(target)]
    ma20 = s.rolling(20).mean()
    out = {
        'index': '沪深300',
        'date': s.index[-1].strftime('%Y-%m-%d') if len(s) else None,
        'close': round(float(s.iloc[-1]), 2) if len(s) else None,
        'ma20': round(float(ma20.iloc[-1]), 2) if len(s) >= 20 else None,
    }
    out['above_ma20'] = None if out['ma20'] is None else out['close'] >= out['ma20']
    out['breadth_ma20'] = round(float((f['ma20d'] > 0).mean() * 100), 1)
    out['up_ratio'] = round(float((f['chg'] > 0).mean() * 100), 1)
    out['median_chg'] = round(float(f['chg'].median() * 100), 2)
    return out
