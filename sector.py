"""Sector standing (板块地位): how many stocks in each industry closed up 5% or more.

The count of strong stocks measures how much money is working a sector at once,
which an average return can't (one limit-up stock lifts an average). Alongside it:

    share   = sector count / whole-market count   (standing, independent of a broad rally)
    purity  = sector count / members traded        (how much of the sector moved)
    lead    = turnover-weighted change − average   (> 0: the large names are leading)
    top_days= sessions in the last LOOKBACK where the sector ranked in the TOP by count

Industries come from the same one-industry-per-stock map as the RPS table, so a stock
is counted once and sector counts add up to the market count.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

THRESH = 0.05  # 涨幅 ≥ 5%
LOOKBACK = 5  # sessions of history shown and used for persistence
TOP = 5  # a sector "ranks" when it is in the top TOP by count
MAIN_DAYS = 3  # top-TOP on at least this many of the LOOKBACK sessions → 主线
MIN_MEMBERS = 8  # smaller sectors are listed but never ranked
MIN_BARS = 20  # skip the first sessions after an IPO (unlimited moves)


def limit_pct(code):
    """Daily price limit by board (ST names are excluded before this is used)."""
    return 0.20 if code.startswith(('30', '68')) else 0.10


def daily_changes(bars, codes, sessions):
    """Wide frame: last `sessions` market dates × codes, forward-adjusted daily change."""
    b = bars[bars['thscode'].isin(codes)].sort_values(['thscode', 'date'])
    chg = b.groupby('thscode')['adj'].pct_change()
    wide = pd.DataFrame({'thscode': b['thscode'], 'date': b['date'], 'chg': chg}).pivot(
        index='date', columns='thscode', values='chg'
    )
    return wide.iloc[-sessions:]


def standing(bars, f, names, industry):
    """Per-industry stats for the last LOOKBACK sessions. `f` is rps.features(bars)."""
    universe = f.join(names[['name']], how='inner')
    st = universe['name'].astype(str).str.upper().str.contains('ST|退')
    universe = universe[~st & (universe['bars'] >= MIN_BARS)]
    codes = [c for c in universe.index if industry.get(c)]
    if not codes:
        return {'dates': [], 'market': [], 'rows': []}

    wide = daily_changes(bars, codes, LOOKBACK)
    dates = [d.strftime('%Y-%m-%d') for d in wide.index]
    ind = pd.Series({c: industry[c] for c in codes})
    weight = universe.loc[codes, 'amt20'].fillna(0.0).clip(lower=0.0)
    limits = pd.Series({c: limit_pct(c) for c in codes})

    strong = wide >= THRESH
    limit_up = wide.ge(limits - 0.003, axis=1)  # limit prices are rounded to the cent
    traded = wide.notna()
    market = strong.sum(axis=1).astype(int).tolist()

    def by_ind(frame):
        return frame.T.groupby(ind).sum().T  # dates × industry

    cnt5 = by_ind(strong)
    zt = by_ind(limit_up)
    n = by_ind(traded)
    up = by_ind(wide > 0)
    total = by_ind(wide.fillna(0.0))
    wsum = by_ind(wide.fillna(0.0) * weight)
    wbase = by_ind(traded * weight)
    avg = total / n.replace(0, np.nan)
    wavg = wsum / wbase.replace(0, np.nan)

    # Rank by count each session (ties: higher purity first); small sectors don't rank.
    eligible = n >= MIN_MEMBERS
    purity_all = cnt5 / n.replace(0, np.nan)
    rank = pd.DataFrame(np.nan, index=cnt5.index, columns=cnt5.columns)
    for d in cnt5.index:
        ok = eligible.loc[d] & (cnt5.loc[d] > 0)
        order = (
            pd.DataFrame({'c': cnt5.loc[d][ok], 'p': purity_all.loc[d][ok]})
            .sort_values(['c', 'p'], ascending=False)
            .index
        )
        rank.loc[d, order] = np.arange(1, len(order) + 1)
    in_top = rank <= TOP

    last = cnt5.index[-1]
    rows = []
    for name in cnt5.columns:
        members = int(n.loc[last, name])
        c = int(cnt5.loc[last, name])
        top_days = int(in_top[name].sum())
        r = rank.loc[last, name]
        a, w = avg.loc[last, name], wavg.loc[last, name]
        today_top = bool(in_top.loc[last, name])
        tag = ''
        if top_days >= MAIN_DAYS:
            tag = '主线' if today_top else '退潮'
        elif today_top and top_days == 1:
            tag = '新晋'
        rows.append(
            {
                'name': name,
                'rank': None if np.isnan(r) else int(r),
                'n': members,
                'cnt5': c,
                'share': round(c / market[-1] * 100, 1) if market[-1] else None,
                'purity': round(c / members * 100, 1) if members else None,
                'up': int(up.loc[last, name]),
                'zt': int(zt.loc[last, name]),
                'avg': None if pd.isna(a) else round(float(a) * 100, 2),
                'wavg': None if pd.isna(w) else round(float(w) * 100, 2),
                'lead': None if pd.isna(a) or pd.isna(w) else round(float(w - a) * 100, 2),
                'hist': [int(v) for v in cnt5[name]],
                'ranks': [None if np.isnan(v) else int(v) for v in rank[name]],
                'top_days': top_days,
                'tag': tag,
            }
        )
    rows.sort(key=lambda x: (x['rank'] is None, x['rank'] or 0, -x['cnt5']))
    return {
        'dates': dates,
        'market': market,
        'rows': rows,
        'params': {'thresh': THRESH * 100, 'lookback': LOOKBACK, 'top': TOP, 'main_days': MAIN_DAYS, 'min_members': MIN_MEMBERS},
    }
