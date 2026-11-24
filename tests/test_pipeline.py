import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import build  # noqa: E402
import data  # noqa: E402
import rps  # noqa: E402


def bars_of(code, closes, start='2026-01-05'):
    dates = pd.bdate_range(start, periods=len(closes))
    return pd.DataFrame(
        {
            'thscode': code,
            'date': dates,
            'open': closes,
            'high': closes,
            'low': closes,
            'close': closes,
            'volume': 1e6,
            'turnover': 1e8,
        }
    )


class AdjustTest(unittest.TestCase):
    def test_cash_dividend_and_bonus(self):
        # 10 → ex-date: 1 CNY dividend and 10送5 → reference prev close (10-1)/1.5 = 6
        b = bars_of('600000.SH', [10.0, 10.0, 6.0, 6.6])
        ev = pd.DataFrame(
            {
                'thscode': ['600000.SH'],
                'ex_date': [b['date'].iloc[2]],
                'dividend_per_share': [1.0],
                'per_share_bonus': [0.5],
                'allotment_ratio': [0.0],
                'allotment_price': [0.0],
            }
        )
        out = data.adjust(b, ev)
        np.testing.assert_allclose(out['adj'], [6.0, 6.0, 6.0, 6.6])
        np.testing.assert_allclose(out['close'], [10.0, 10.0, 6.0, 6.6])  # raw untouched

    def test_rights_issue_and_suspension(self):
        # Ex-date falls during a suspension: the last bar before it is used.
        b = bars_of('000001.SZ', [20.0, 20.0, 15.0])
        b.loc[2, 'date'] = b['date'].iloc[1] + pd.Timedelta(days=7)
        ex = b['date'].iloc[1] + pd.Timedelta(days=3)
        ev = pd.DataFrame(
            {
                'thscode': ['000001.SZ'],
                'ex_date': [ex],
                'dividend_per_share': [0.0],
                'per_share_bonus': [0.0],
                'allotment_ratio': [0.3],
                'allotment_price': [10.0],
            }
        )
        out = data.adjust(b, ev)
        ref = (20 + 10 * 0.3) / 1.3
        np.testing.assert_allclose(out['adj'][:2], [ref, ref])
        self.assertAlmostEqual(out['adj'].iloc[2], 15.0)

    def test_events_outside_window_and_other_stocks_ignored(self):
        b = pd.concat([bars_of('600000.SH', [10, 11, 12]), bars_of('600001.SH', [5, 5, 5])])
        ev = pd.DataFrame(
            {
                'thscode': ['600000.SH', '600001.SH'],
                'ex_date': [pd.Timestamp('2020-01-01'), b['date'].iloc[-1]],
                'dividend_per_share': [1.0, 0.5],
                'per_share_bonus': [0.0, 0.0],
                'allotment_ratio': [0.0, 0.0],
                'allotment_price': [0.0, 0.0],
            }
        )
        out = data.adjust(b, ev).set_index(['thscode', 'date'])
        np.testing.assert_allclose(out.loc['600000.SH', 'adj'], [10, 11, 12])
        np.testing.assert_allclose(out.loc['600001.SH', 'adj'], [4.5, 4.5, 5])


class StoreTest(unittest.TestCase):
    def test_read_dump_filters_codes_and_converts_dates(self):
        day = pd.Timestamp('2026-11-24')
        ms = int((day - pd.Timedelta(hours=8)).value // 1_000_000)
        table = pa.table(
            {
                'thscode': ['600519.SH', '830799.BJ', '200002.SZ', '300750.SZ'],
                'currency': ['CNY'] * 4,
                'interval': ['1d'] * 4,
                'adjusted': ['none'] * 4,
                'date_ms': [ms] * 4,
                'open_price': [1.0] * 4,
                'high_price': [1.0] * 4,
                'low_price': [1.0] * 4,
                'close_price': [1.0, 1.0, 1.0, 0.0],
                'volume': [1.0] * 4,
                'turnover': [1.0] * 4,
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'd.parquet'
            pq.write_table(table, path)
            df = data.read_dump_bars(path)
        self.assertEqual(df['thscode'].tolist(), ['600519.SH'])  # BJ, B share, zero close dropped
        self.assertEqual(df['date'].iloc[0], day)

    def test_merge_prefers_fresh_and_trims(self):
        old = bars_of('600000.SH', [1.0, 2.0, 3.0])
        fresh = bars_of('600000.SH', [9.0, 4.0], start=old['date'].iloc[2])
        merged = data.merge(old, fresh, keep=3)
        self.assertEqual(merged['close'].tolist(), [2.0, 9.0, 4.0])
        self.assertFalse(data.needs_full(old, fresh))
        self.assertTrue(data.needs_full(old, bars_of('600000.SH', [1.0], start='2026-06-01')))
        self.assertTrue(data.needs_full(None, fresh))


class ModelTest(unittest.TestCase):
    def test_rank_selects_and_excludes(self):
        bars, names, idx, industry, caps = build.synthetic(n_stocks=3400, seed=3)
        bars = data.adjust(bars, None)
        f = rps.features(bars)
        target = bars['date'].max()
        elig, dropped = rps.eligible(f, names, target)
        self.assertGreater(dropped['ST/退市'], 0)
        self.assertGreater(dropped['上市不满一年'], 0)
        self.assertFalse(elig['name'].str.contains('ST').any())
        f = rps.rank(elig)
        self.assertAlmostEqual(f['rps120'].max(), 100.0)
        # Highest 120-day return carries RPS 100; ranking is monotonic in the return.
        self.assertEqual(f['r120'].idxmax(), f['rps120'].idxmax())
        sel = f[f['inB']]
        self.assertTrue(((sel['rps120'] > 90) & (sel['rps250'] > 85)).all())
        expected = f[(f['rps50'] > 90) & (f['rps120'] > 85)].index
        self.assertEqual(sorted(f.index[f['inA']]), sorted(expected))

    def test_return_uses_own_bars(self):
        f = rps.features(data.adjust(bars_of('600000.SH', list(range(1, 301))), None))
        self.assertAlmostEqual(f.loc['600000.SH', 'r50'], 300 / 250 - 1)
        self.assertAlmostEqual(f.loc['600000.SH', 'r250'], 300 / 50 - 1)
        self.assertAlmostEqual(f.loc['600000.SH', 'ma20'], np.mean(range(281, 301)))

    def test_states(self):
        self.assertEqual(rps.state({'ma20d': -0.01, 'pull10': -0.02}), '跌破MA20')
        self.assertEqual(rps.state({'ma20d': 0.01, 'pull10': -0.06}), '回踩中')
        self.assertEqual(rps.state({'ma20d': 0.01, 'pull10': -0.01}), '高位强势')

    def test_streaks(self):
        history = {'2026-11-20': {'A': ['x', 'y']}, '2026-11-23': {'A': ['x']}}
        s, prev = rps.streaks(history, '2026-11-24', {'A': ['x', 'y', 'z']})
        self.assertEqual(prev, '2026-11-23')
        self.assertEqual(s['A'], {'x': 3, 'y': 1, 'z': 1})


class BuildTest(unittest.TestCase):
    def test_dataset_changes_and_render(self):
        bars, names, idx, industry, caps = build.synthetic(n_stocks=3400, seed=5)
        bars = data.adjust(bars, None)
        logs = []
        first, target, sets = build.assemble(bars, names, idx, industry, caps, logs.append, {})
        self.assertIsNone(first['meta']['previous'])
        prev = sorted(sets['A'])[:-3] + ['999999.SH']
        history = {'2026-11-23': {'A': prev, 'B': sets['B']}}
        ds, _, _ = build.assemble(bars, names, idx, industry, caps, logs.append, history)
        self.assertEqual(ds['meta']['previous'], '2026-11-23')
        new_codes = [c for c, n in zip(ds['cols']['code'], ds['cols']['newA']) if n]
        self.assertEqual(len(new_codes), 3)
        self.assertEqual([c for c, _ in ds['exits']['A']], ['999999'])
        self.assertFalse(any(ds['cols']['newB']))
        json.dumps(ds, allow_nan=False)  # page JSON must be strict
        with tempfile.TemporaryDirectory() as tmp:
            page = build.render(ds, Path(tmp)).read_text()
        self.assertIn('RPS VIEW', page)
        self.assertNotIn('__DATA__', page)
        self.assertNotIn('</script><script>__', page)

    def test_refuses_shrunken_universe(self):
        bars, names, idx, industry, caps = build.synthetic(n_stocks=500, seed=1)
        with self.assertRaises(RuntimeError):
            build.assemble(data.adjust(bars, None), names, idx, industry, caps, lambda m: None, {})


if __name__ == '__main__':
    unittest.main()
