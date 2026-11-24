"""Market data: a local store of raw daily bars kept current with the 10-day dump,
forward adjustment from corporate-action events, and reference data."""

from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from hithink import AuthError

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / 'data_cache'
STORE = CACHE / 'bars.parquet'
INDUSTRY = CACHE / 'industry.json'
TZ = dt.timezone(dt.timedelta(hours=8))
KEEP_SESSIONS = 420  # enough for 250-day returns, 250-day highs and some slack
BAR_COLS = ['thscode', 'date', 'open', 'high', 'low', 'close', 'volume', 'turnover']
# Shanghai/Shenzhen main boards, ChiNext and STAR. Beijing exchange and B shares are excluded.
CODE_RE = re.compile(r'^(60\d{4}\.SH|68\d{4}\.SH|00\d{4}\.SZ|30\d{4}\.SZ)$')
INDUSTRY_MAX_AGE_DAYS = 7


def ms_to_date(ms):
    """Dump timestamps are Asia/Shanghai midnight in milliseconds."""
    return pd.to_datetime(np.asarray(ms, dtype='int64') + 8 * 3600 * 1000, unit='ms').normalize()


# ── bars ──────────────────────────────────────────────────────────────────────


def read_dump_bars(path, since=None):
    """Read a daily-k dump into the store schema (raw, unadjusted prices)."""
    cols = ['thscode', 'date_ms', 'open_price', 'high_price', 'low_price', 'close_price', 'volume', 'turnover']
    filters = None
    if since is not None:
        since_ms = int((pd.Timestamp(since) - pd.Timedelta(hours=8)).value // 1_000_000)
        filters = [('date_ms', '>=', since_ms)]
    df = pq.read_table(path, columns=cols, filters=filters).to_pandas()
    df = df[df['thscode'].astype(str).str.match(CODE_RE)]
    df = df.rename(
        columns={'open_price': 'open', 'high_price': 'high', 'low_price': 'low', 'close_price': 'close'}
    )
    df['date'] = ms_to_date(df.pop('date_ms'))
    for c in ('open', 'high', 'low', 'close', 'volume', 'turnover'):
        df[c] = pd.to_numeric(df[c], errors='coerce')
    df = df[(df['close'] > 0) & np.isfinite(df['close'])]
    return df[BAR_COLS].reset_index(drop=True)


def trim(df, keep=KEEP_SESSIONS):
    df = df.drop_duplicates(['thscode', 'date'], keep='last')
    dates = np.sort(df['date'].unique())
    if len(dates) > keep:
        df = df[df['date'] >= dates[-keep]]
    return df.sort_values(['thscode', 'date']).reset_index(drop=True)


def merge(store, fresh, keep=KEEP_SESSIONS):
    """Overlapping (thscode, date) rows take the fresh dump's values."""
    if store is None or store.empty:
        return trim(fresh, keep)
    return trim(pd.concat([store, fresh], ignore_index=True), keep)


def needs_full(store, fresh):
    """The 10-day dump only bridges a store that already reaches its first date."""
    if store is None or store.empty:
        return True
    return store['date'].max() < fresh['date'].min()


def update_store(client, log, workdir=CACHE):
    """Bring the local store up to the latest dump. Returns the merged bars."""
    store = pd.read_parquet(STORE) if STORE.exists() else None
    tenday = read_dump_bars(client.download_dump('daily-k-10d', workdir / 'daily_k_10d.parquet'))
    log(f'近10日增量：{len(tenday):,} 行，{tenday["date"].min():%Y-%m-%d} ~ {tenday["date"].max():%Y-%m-%d}')
    if needs_full(store, tenday):
        reason = '首次运行' if store is None else f'本地数据停在 {store["date"].max():%Y-%m-%d}，超出增量窗口'
        log(f'{reason}，改用全量日K')
        path = client.download_dump('daily-k', workdir / 'daily_k_full.parquet')
        since = tenday['date'].max() - pd.Timedelta(days=int(KEEP_SESSIONS * 1.6))
        store = read_dump_bars(path, since=since)
        path.unlink(missing_ok=True)
        log(f'全量日K：保留 {len(store):,} 行')
    bars = merge(store, tenday)
    STORE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STORE.with_suffix('.tmp')
    bars.to_parquet(tmp, index=False)
    tmp.replace(STORE)
    return bars


# ── forward adjustment ────────────────────────────────────────────────────────


def read_events(path):
    df = pq.read_table(path).to_pandas()
    df = df[df['thscode'].astype(str).str.match(CODE_RE)].copy()
    df['ex_date'] = ms_to_date(df['ex_date_ms'])
    for c in ('dividend_per_share', 'per_share_bonus', 'allotment_ratio', 'allotment_price'):
        df[c] = pd.to_numeric(df[c], errors='coerce').fillna(0.0) if c in df else 0.0
    return df[['thscode', 'ex_date', 'dividend_per_share', 'per_share_bonus', 'allotment_ratio', 'allotment_price']]


def adjust(bars, events):
    """Add `adj` (forward-adjusted close) to bars.

    For an ex-date with dividend d, bonus ratio b, rights ratio r at price p, the
    exchange's reference previous close is (C - d + p·r) / (1 + b + r). Every bar
    before the ex-date is scaled by reference / C, so returns across the event are
    true holding returns and the latest bar keeps its traded price.
    """
    bars = bars.sort_values(['thscode', 'date']).reset_index(drop=True)
    bars['adj'] = bars['close'].astype(float)
    if events is None or events.empty:
        return bars
    lo, hi = bars['date'].min(), bars['date'].max()
    ev = events[(events['ex_date'] > lo) & (events['ex_date'] <= hi)]
    ev = ev.groupby(['thscode', 'ex_date'], as_index=False).sum(numeric_only=True)
    if ev.empty:
        return bars
    # Locate the last bar strictly before each ex-date (suspensions included).
    left = bars[['thscode', 'date', 'close']].rename(columns={'date': 'prev_date'}).sort_values('prev_date')
    ev = ev.sort_values('ex_date')
    ev['key'] = ev['ex_date'] - pd.Timedelta(days=1)
    m = pd.merge_asof(ev, left, left_on='key', right_on='prev_date', by='thscode', direction='backward')
    m = m.dropna(subset=['close'])
    ref = (m['close'] - m['dividend_per_share'] + m['allotment_price'] * m['allotment_ratio']) / (
        1 + m['per_share_bonus'] + m['allotment_ratio']
    )
    m['ratio'] = (ref / m['close']).where(lambda s: (s > 0) & (s <= 1.0 + 1e-9))
    m = m.dropna(subset=['ratio'])
    mult = m.groupby(['thscode', 'prev_date'])['ratio'].prod().rename('mult')
    bars = bars.join(mult, on=['thscode', 'date'])
    bars['mult'] = bars['mult'].fillna(1.0)
    # factor_t = product of multipliers on and after t, within each stock.
    rev = bars.iloc[::-1]
    factor = rev.groupby('thscode', sort=False)['mult'].cumprod().iloc[::-1]
    bars['adj'] = bars['close'] * factor
    return bars.drop(columns='mult')


def load_events(client, workdir=CACHE):
    return read_events(client.download_dump('adjustment-factors', workdir / 'adjustment_factors.parquet'))


# ── reference data ────────────────────────────────────────────────────────────


def tickers(client):
    items = client.paged('/api/meta/tickers/list', {'asset_type': 'a-share'}, limit=5000)
    rows = [
        {'thscode': i['thscode'], 'name': i.get('name') or '', 'list_date': i.get('list_date')}
        for i in items
        if CODE_RE.match(str(i.get('thscode', '')))
    ]
    return pd.DataFrame(rows).drop_duplicates('thscode').set_index('thscode')


def float_caps(client, codes):
    """流通市值 (CNY) from the final auction snapshot, 100 codes per call."""
    out = {}
    codes = list(codes)
    for i in range(0, len(codes), 100):
        data = client.get('/api/a-share/auction/snapshot', {'thscodes': ','.join(codes[i : i + 100]), 'stage': 'final'})
        for item in data.get('item') or []:
            cap = item.get('float_market_cap')
            if isinstance(cap, (int, float)) and cap > 0:
                out[item['thscode']] = float(cap)
    return out


def index_closes(client, thscode='000300.SH', days=120):
    end = dt.datetime.now(TZ)
    start = end - dt.timedelta(days=days)
    data = client.get(
        '/api/a-share-index/prices/historical',
        {
            'thscode': thscode,
            'interval': '1d',
            'start': int(start.timestamp() * 1000),
            'end': int(end.timestamp() * 1000),
        },
    )
    items = [i for i in data.get('item') or [] if isinstance(i.get('close_price'), (int, float))]
    s = pd.Series(
        [i['close_price'] for i in items], index=ms_to_date([i['date_ms'] for i in items]), dtype=float
    ).sort_index()
    return s[~s.index.duplicated(keep='last')]


def pick_industry(memberships, sizes):
    """Choose one industry per stock: the finest board (fewest members) that still has
    at least 8 members, so tiny third-level boards don't fragment the summary."""
    out = {}
    for code, boards in memberships.items():
        ranked = sorted(boards, key=lambda b: sizes[b])
        usable = [b for b in ranked if sizes[b] >= 8]
        out[code] = (usable or ranked)[0]
    return out


def industries(client, log, today):
    """Map thscode → 同花顺行业 name; refreshed weekly and cached."""
    if INDUSTRY.exists():
        cached = json.loads(INDUSTRY.read_text())
        age = (today - dt.date.fromisoformat(cached['fetched'])).days
        if age < INDUSTRY_MAX_AGE_DAYS and cached.get('map'):
            return cached['map'], cached['fetched']
    try:
        boards = client.get('/api/a-share-index/catalog/ths-index-list', {'tag': 'industry'}).get('item') or []
        names, members = {}, {}
        for b in boards:
            names[b['thscode']] = b['name']
            items = client.get('/api/a-share-index/constituents/ths-stock-list', {'thscode': b['thscode']}).get('item')
            members[b['thscode']] = [i['thscode'] for i in items or []]
        sizes = {b: len(v) for b, v in members.items()}
        memberships = {}
        for board, codes in members.items():
            if sizes[board] == 0:
                continue
            for code in codes:
                memberships.setdefault(code, []).append(board)
        mapping = {code: names[b] for code, b in pick_industry(memberships, sizes).items()}
        INDUSTRY.parent.mkdir(parents=True, exist_ok=True)
        INDUSTRY.write_text(json.dumps({'fetched': today.isoformat(), 'map': mapping}, ensure_ascii=False))
        log(f'行业分类已刷新：{len(boards)} 个同花顺行业指数，覆盖 {len(mapping):,} 只')
        return mapping, today.isoformat()
    except AuthError:
        raise
    except Exception as exc:  # industry is decoration; never block the publish on it
        if INDUSTRY.exists():
            cached = json.loads(INDUSTRY.read_text())
            log(f'行业分类刷新失败，沿用 {cached["fetched"]} 的缓存：{exc}')
            return cached['map'], cached['fetched']
        log(f'行业分类不可用：{exc}')
        return {}, None
