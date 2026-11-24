"""Build the A-share RPS dashboard: update data, rank, write index.html.

    python build.py              # fetch from 同花顺 (HITHINK_API_KEY) and render
    python build.py --render     # re-render the cached snapshot, no network
    python build.py --synthetic  # fake market, for checking the page and pipeline
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

import data
import rps

ROOT = Path(__file__).resolve().parent
HISTORY = ROOT / 'track/history.json'
SNAPSHOT = data.CACHE / 'snapshot.json'
OUT = ROOT / 'public'
HISTORY_KEEP = 300  # snapshot days kept in track/history.json
MIN_RANKED = 3000  # refuse to publish a shrunken universe (percentiles would drift)


class Log:
    def __init__(self):
        self.lines = []

    def __call__(self, msg):
        stamp = dt.datetime.now(data.TZ).strftime('%H:%M:%S')
        print(f'[{stamp}] {msg}', flush=True)
        self.lines.append(f'{stamp} {msg}')


def clean(v, digits=None):
    if v is None or (isinstance(v, float) and not math.isfinite(v)):
        return None
    if isinstance(v, (np.floating, float)):
        v = float(v)
        if not math.isfinite(v):
            return None
        return round(v, digits) if digits is not None else v
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.bool_,)):
        return bool(v)
    return v


def load_history():
    return json.loads(HISTORY.read_text()) if HISTORY.exists() else {}


def save_history(history, target, sets):
    history[target] = sets
    keep = sorted(history)[-HISTORY_KEEP:]
    HISTORY.parent.mkdir(parents=True, exist_ok=True)
    HISTORY.write_text(json.dumps({d: history[d] for d in keep}, ensure_ascii=False, separators=(',', ':')) + '\n')


def assemble(bars, names, index_close, industry, caps_fn, log, history):
    """Rank the market and build the page dataset. `caps_fn(codes)` returns float caps."""
    target = bars['date'].max().strftime('%Y-%m-%d')
    f = rps.features(bars)
    f, dropped = rps.eligible(f, names, target)
    for reason, n in sorted(dropped.items()):
        log(f'剔除{reason}：{n:,} 只')
    f = rps.rank(f)
    ranked = int(f['r120'].notna().sum())
    log(f'参与排名：{len(f):,} 只（RPS250 有 {int(f["r250"].notna().sum()):,} 只满 251 根）')
    if ranked < MIN_RANKED:
        raise RuntimeError(f'参与排名仅 {ranked} 只，低于 {MIN_RANKED}，停止发布以免百分位口径漂移')

    codes_by_set = {k: sorted(f.index[f[f'in{k}']]) for k in rps.SETS}
    for k, codes in codes_by_set.items():
        log(f'{rps.SETS[k]["label"]}：入选 {len(codes)} 只')
    selected = sorted(set().union(*codes_by_set.values()))
    caps = caps_fn(selected) if selected else {}
    log(f'流通市值：取得 {len(caps)}/{len(selected)} 只入选股')

    streak, prev = rps.streaks(history, target, codes_by_set)
    prev_sets = history.get(prev, {}) if prev else {}
    changes = {}
    for k, codes in codes_by_set.items():
        before = set(prev_sets.get(k, []))
        changes[k] = {
            'new': [c for c in codes if c not in before] if prev else [],
            'out': sorted(c for c in before if c not in set(codes)),
        }

    f['ind'] = [industry.get(c, '') for c in f.index]
    f['cap'] = [caps.get(c) for c in f.index]
    f['state'] = f.apply(rps.state, axis=1)
    f = f.sort_values('rps120', ascending=False)

    cols = {
        'code': [c.split('.')[0] for c in f.index],
        'ex': [c.split('.')[1] for c in f.index],
        'name': f['name'].tolist(),
        'ind': f['ind'].tolist(),
        'close': [clean(v, 3) for v in f['close']],
        'chg': [clean(v * 100, 2) for v in f['chg']],
        'amt20': [clean(v / 1e8, 2) for v in f['amt20']],
        'cap': [clean(v / 1e8, 1) if v else None for v in f['cap']],
        'ma20d': [clean(v * 100, 2) for v in f['ma20d']],
        'pull10': [clean(v * 100, 2) for v in f['pull10']],
        'hi250d': [clean(v * 100, 2) for v in f['hi250d']],
        'state': f['state'].tolist(),
    }
    for n in rps.PERIODS:
        cols[f'r{n}'] = [clean(v * 100, 2) for v in f[f'r{n}']]
        cols[f'rps{n}'] = [clean(v, 2) for v in f[f'rps{n}']]
    for k in rps.SETS:
        cols[f'in{k}'] = [bool(v) for v in f[f'in{k}']]
        cols[f'streak{k}'] = [streak[k].get(c, 0) for c in f.index]
        cols[f'new{k}'] = [c in set(changes[k]['new']) for c in f.index]

    industry_summary = {}
    for k in rps.SETS:
        sel = f[f[f'in{k}']]
        grp = sel.assign(ind=sel['ind'].replace('', '未分类')).groupby('ind')
        summary = pd.DataFrame({'n': grp.size(), 'rps': grp[f"rps{rps.SETS[k]['short']}"].mean()})
        industry_summary[k] = [
            [name, int(r['n']), clean(r['rps'], 1)] for name, r in summary.sort_values('n', ascending=False).iterrows()
        ]

    name_of = dict(zip(f.index, f['name']))
    exits = {
        k: [[c.split('.')[0], name_of.get(c, '')] for c in v['out']] for k, v in changes.items()
    }
    dataset = {
        'meta': {
            'target': target,
            'generated_at': dt.datetime.now(data.TZ).isoformat(timespec='seconds'),
            'previous': prev,
            'ranked': len(f),
            'sets': rps.SETS,
            'periods': rps.PERIODS,
            'min_cap': rps.MIN_FLOAT_CAP / 1e8,
            'pullback': rps.PULLBACK * 100,
            'dropped': dropped,
        },
        'market': rps.market_state(index_close, target, f),
        'cols': cols,
        'industry': industry_summary,
        'exits': exits,
    }
    return dataset, target, codes_by_set


def render(dataset, out=OUT):
    template = (ROOT / 'dashboard.html').read_text(encoding='utf-8')
    for marker, file in (('__STYLE__', 'app.css'), ('__SCRIPT__', 'app.js')):
        template = template.replace(marker, (ROOT / file).read_text(encoding='utf-8'))
    encoded = json.dumps(dataset, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    page = template.replace('__DATA__', encoded.replace('<', '\\u003c'))
    out.mkdir(parents=True, exist_ok=True)
    (out / 'index.html').write_text(page, encoding='utf-8')
    return out / 'index.html'


# ── synthetic market (no network) ────────────────────────────────────────────


def synthetic(n_stocks=4200, sessions=data.KEEP_SESSIONS, seed=7):
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(end=pd.Timestamp('2026-11-24'), periods=sessions)
    prefixes = ['600', '601', '603', '000', '002', '300', '688']
    codes = []
    for i in range(n_stocks):
        p = prefixes[i % len(prefixes)]
        codes.append(f'{p}{i // len(prefixes):03d}' + ('.SH' if p.startswith('6') else '.SZ'))
    codes = sorted(set(codes))
    frames = []
    inds = ['半导体', '通信设备', '汽车零部件', '电力', '银行', '医疗器械', '软件开发', '白酒', '光伏设备', '游戏']
    for j, code in enumerate(codes):
        drift = rng.normal(0.0002, 0.0012)
        vol = rng.uniform(0.012, 0.035)
        r = rng.normal(drift, vol, sessions)
        close = 10 * np.exp(np.cumsum(r))
        turnover = rng.lognormal(19.5, 1.0) * np.ones(sessions)
        frames.append(
            pd.DataFrame(
                {
                    'thscode': code,
                    'date': dates,
                    'open': close,
                    'high': close * 1.01,
                    'low': close * 0.99,
                    'close': close.round(2),
                    'volume': 1e6,
                    'turnover': turnover,
                }
            )
        )
    bars = pd.concat(frames, ignore_index=True)
    names = pd.DataFrame(
        {
            'name': [f'样本{c[:6]}' if k % 40 else f'*ST样本{c[:6]}' for k, c in enumerate(codes)],
            'list_date': ['2015-01-05' if k % 25 else '2026-06-01' for k in range(len(codes))],
        },
        index=pd.Index(codes, name='thscode'),
    )
    industry = {c: inds[k % len(inds)] for k, c in enumerate(codes)}
    idx = pd.Series(4000 * np.exp(np.cumsum(rng.normal(0, 0.01, sessions))), index=dates)
    caps = lambda sel: {c: float(rng.uniform(10e8, 800e8)) for c in sel}  # noqa: E731
    return bars, names, idx, industry, caps


# ── main ────────────────────────────────────────────────────────────────────


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--render', action='store_true', help='re-render the cached snapshot without network')
    ap.add_argument('--synthetic', action='store_true', help='fake market data; does not touch history')
    args = ap.parse_args(argv)
    log = Log()
    t0 = time.time()

    if args.render:
        dataset = json.loads(SNAPSHOT.read_text())
        print(render(dataset))
        return 0

    if args.synthetic:
        bars, names, idx, industry, caps = synthetic()
        bars = data.adjust(bars, None)
        dataset, target, sets = assemble(bars, names, idx, industry, caps, log, {})
        dataset['meta']['synthetic'] = True
        dataset['log'] = log.lines
        print(render(dataset))
        return 0

    import hithink

    client = hithink.Client()
    bars = data.update_store(client, log)
    target = bars['date'].max()
    log(f'目标交易日：{target:%Y-%m-%d}，股票 {bars["thscode"].nunique():,} 只')
    events = data.load_events(client)
    log(f'复权事件：{len(events):,} 条')
    bars = data.adjust(bars, events)
    names = data.tickers(client)
    log(f'代码表：{len(names):,} 只')
    idx = data.index_closes(client)
    if len(idx) and idx.index[-1] > target:
        log(f'注意：沪深300已有 {idx.index[-1]:%Y-%m-%d} 行情，日K导出仍停在 {target:%Y-%m-%d}')
    industry, industry_date = data.industries(client, log, target.date())
    history = load_history()
    dataset, target_s, sets = assemble(
        bars, names, idx, industry, lambda c: data.float_caps(client, c), log, history
    )
    dataset['meta']['industry_date'] = industry_date
    dataset['meta']['api_calls'] = client.calls
    log(f'接口调用 {client.calls} 次，用时 {time.time() - t0:.0f} 秒')
    dataset['log'] = log.lines
    save_history(history, target_s, sets)
    SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT.write_text(json.dumps(dataset, ensure_ascii=False, separators=(',', ':')))
    print(render(dataset))
    return 0


if __name__ == '__main__':
    sys.exit(main())
