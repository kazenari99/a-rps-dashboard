(() => {
  'use strict';
  const D = JSON.parse(document.getElementById('dataset').textContent);
  const $ = (id) => document.getElementById(id);
  const esc = (s) =>
    String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
  const store = {
    get(k, d) {
      try {
        const v = localStorage.getItem('rps-view:' + k);
        return v === null ? d : JSON.parse(v);
      } catch {
        return d;
      }
    },
    set(k, v) {
      try {
        localStorage.setItem('rps-view:' + k, JSON.stringify(v));
      } catch {
        /* storage unavailable: keep in memory only */
      }
    },
  };

  // Columnar dataset → row objects.
  const C = D.cols;
  const rows = C.code.map((_, i) => {
    const r = {};
    for (const k in C) r[k] = C[k][i];
    return r;
  });
  const byCode = new Map(rows.map((r) => [r.code, r]));
  const SETS = D.meta.sets;
  const KEYS = Object.keys(SETS);

  const state = {
    set: KEYS.includes(store.get('set')) ? store.get('set') : 'B',
    q: '',
    ind: 'all',
    st: 'all',
    cap: true,
    watch: false,
    sort: null,
    dir: -1,
    limit: 300,
  };
  const favs = new Set(store.get('favs', []));
  const SEC = D.sector || { dates: [], market: [], rows: [] };
  const SECP = SEC.params || { thresh: 5, lookback: 5, top: 5, main_days: 3, min_members: 8 };
  const secByName = new Map(SEC.rows.map((r) => [r.name, r]));
  let secAll = false;
  let secOpen = null;

  const fmt = (v, d = 2, sign = false) => {
    if (v === null || v === undefined || Number.isNaN(v)) return '—';
    const s = Number(v).toFixed(d);
    return sign && v > 0 ? '+' + s : s;
  };
  const pctCell = (v, d = 1) =>
    `<td class="num ${v > 0 ? 'positive' : v < 0 ? 'negative' : ''}">${v === null || v === undefined ? '—' : fmt(v, d, true) + '%'}</td>`;
  const rpsCell = (v, min) => {
    if (v === null || v === undefined) return '<td class="num rps-off">—</td>';
    const ok = v > min;
    return `<td class="num"><div class="rps-cell"><span class="${ok ? '' : 'rps-off'}">${fmt(v, 1)}</span><div class="rps-bar"><i style="width:${Math.max(0, Math.min(100, v))}%;${ok ? '' : 'background:#3a4a63'}"></i></div></div></td>`;
  };
  const chipClass = { 回踩中: 'pull', 高位强势: 'strong', 跌破MA20: 'broken' };

  // ── header / freshness ────────────────────────────────────────────────
  function bjNow() {
    const now = new Date();
    return new Date(now.getTime() + (now.getTimezoneOffset() + 480) * 60000);
  }
  function businessDaysBetween(a, b) {
    let n = 0;
    const d = new Date(a);
    while (d < b) {
      d.setDate(d.getDate() + 1);
      if (d <= b && d.getDay() % 6 !== 0) n++;
    }
    return n;
  }
  function header() {
    const m = D.meta;
    $('asOf').textContent = `数据日期 ${m.target} · 更新于 ${m.generated_at.slice(5, 16).replace('T', ' ')}${m.synthetic ? ' · 合成数据' : ''}`;
    const now = bjNow();
    const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    const [y, mo, d] = m.target.split('-').map(Number);
    const lag = businessDaysBetween(new Date(y, mo - 1, d), today);
    const late = lag > 1 || (lag === 1 && now.getHours() * 60 + now.getMinutes() >= 21 * 60 + 30);
    if (m.synthetic || late) {
      $('staleBanner').hidden = false;
      $('staleBanner').textContent = m.synthetic
        ? '这是用合成数据生成的预览页面，数字没有意义。'
        : `数据可能已过期：页面数据停在 ${m.target}，落后 ${lag} 个工作日（节假日也会触发此提示）。`;
      $('liveDot').classList.add('stale');
    }
  }

  // ── set switch / KPIs / market ──────────────────────────────────────────
  function setSwitch() {
    $('setSwitch').innerHTML = KEYS.map(
      (k) => `<button type="button" role="tab" data-set="${k}" class="${k === state.set ? 'active' : ''}">${esc(SETS[k].label)}</button>`,
    ).join('');
    $('setSwitch').onclick = (e) => {
      const b = e.target.closest('button[data-set]');
      if (!b) return;
      state.set = b.dataset.set;
      store.set('set', state.set);
      state.sort = null;
      state.limit = 300;
      renderAll();
    };
  }
  const cur = () => SETS[state.set];

  function kpis() {
    const s = cur();
    const k = state.set;
    const sel = rows.filter((r) => r['in' + k]);
    const capped = sel.filter((r) => r.cap === null || r.cap >= D.meta.min_cap);
    const fresh = sel.filter((r) => r['new' + k]);
    const pull = sel.filter((r) => r.state === '回踩中');
    const out = (D.exits[k] || []).length;
    const mk = D.market;
    const cards = [
      ['入选', sel.length, '只', `RPS${s.short}>${s.short_min} 且 RPS${s.long}>${s.long_min}`],
      ['市值≥30亿', capped.length, '只', '页面默认筛选'],
      ['今日新进', D.meta.previous ? fresh.length : '—', D.meta.previous ? '只' : '', D.meta.previous ? `退出 ${out} 只 · 对比 ${D.meta.previous}` : '首个快照，暂无对比'],
      ['回踩中', pull.length, '只', `距10日高 ≤ ${D.meta.pullback}% 且在MA20上方`],
      ['参与排名', D.meta.ranked.toLocaleString(), '只', `全市场上涨 ${fmt(mk.up_ratio, 1)}%`],
    ];
    $('kpiGrid').innerHTML = cards
      .map(([l, v, u, f]) => `<article class="kpi-card"><p class="kpi-label">${l}</p><p class="kpi-value">${v}<small>${u}</small></p><p class="kpi-foot">${esc(f)}</p></article>`)
      .join('');
    $('setTitle').textContent = s.label + ' 选股';
    $('setRule').textContent = `短周期 RPS${s.short} > ${s.short_min}，长周期 RPS${s.long} > ${s.long_min}，全市场 ${D.meta.ranked.toLocaleString()} 只一起排名`;
  }

  function market() {
    const m = D.market;
    const strip = document.querySelector('.market-strip');
    if (m.above_ma20 === null) {
      $('regimeTitle').textContent = '沪深300 vs MA20：数据不足';
      $('regimeCopy').textContent = '';
    } else {
      const gap = (m.close / m.ma20 - 1) * 100;
      $('regimeTitle').innerHTML = m.above_ma20
        ? `<span class="positive">风控正常</span> · 沪深300 站上 MA20`
        : `<span class="negative">风控触发</span> · 沪深300 低于 MA20`;
      $('regimeCopy').textContent = `${m.date} 收盘 ${fmt(m.close, 2)} / MA20 ${fmt(m.ma20, 2)}（${fmt(gap, 2, true)}%）。只作提示，名单照常列出。`;
      strip.classList.toggle('alert', !m.above_ma20);
    }
    const bars = [
      ['站上MA20', m.breadth_ma20],
      ['今日上涨', m.up_ratio],
    ];
    $('breadthBars').innerHTML =
      bars
        .map(([l, v]) => `<div class="breadth-item"><span>${l}</span><div class="breadth-track"><div class="breadth-fill" style="width:${v}%"></div></div><b>${fmt(v, 1)}%</b></div>`)
        .join('') + `<div class="breadth-item"><span>涨跌中位数</span><span></span><b class="${m.median_chg >= 0 ? 'positive' : 'negative'}">${fmt(m.median_chg, 2, true)}%</b></div>`;
  }

  function changes() {
    const k = state.set;
    $('comparisonDate').textContent = D.meta.previous ? `${D.meta.previous} → ${D.meta.target}` : '首个快照';
    const fresh = rows.filter((r) => r['new' + k]).sort((a, b) => b['rps' + cur().short] - a['rps' + cur().short]);
    const out = D.exits[k] || [];
    const chip = (code, name, cls) => `<span class="chip ${cls}" data-code="${esc(code)}"><b>${esc(code)}</b>${esc(name)}</span>`;
    const none = (t) => `<p class="empty-note">${t}</p>`;
    $('dailyChanges').innerHTML = `
      <article><span>新进入选 · ${D.meta.previous ? fresh.length + ' 只' : '—'}</span><div class="chips">${
        D.meta.previous ? fresh.map((r) => chip(r.code, r.name, 'up')).join('') || none('没有新进') : none('从下一个交易日开始对比')
      }</div></article>
      <article><span>退出入选 · ${D.meta.previous ? out.length + ' 只' : '—'}</span><div class="chips">${
        D.meta.previous ? out.map(([c, n]) => chip(c, n, 'down')).join('') || none('没有退出') : none('从下一个交易日开始对比')
      }</div></article>`;
    $('dailyChanges').onclick = (e) => {
      const c = e.target.closest('.chip');
      if (!c) return;
      $('searchInput').value = c.dataset.code;
      state.q = c.dataset.code;
      table();
      document.querySelector('.table-section').scrollIntoView({ behavior: 'smooth' });
    };
  }

  // ── table ───────────────────────────────────────────────────────────────
  function columns() {
    const s = cur();
    const k = state.set;
    return [
      { h: '☆', cell: (r) => `<td><button class="favorite ${favs.has(r.code) ? 'on' : ''}" data-fav="${r.code}" aria-label="收藏">${favs.has(r.code) ? '★' : '☆'}</button></td>` },
      {
        h: '代码 / 名称',
        key: 'code',
        cell: (r) =>
          `<td class="ticker-cell"><a href="https://stockpage.10jqka.com.cn/${r.code}/" target="_blank" rel="noopener">${esc(r.name)}</a>${r['new' + k] ? '<span class="new-tag">新</span>' : ''}<small>${r.code}${r['in' + k] ? '' : ' <span class="out-tag">· 未入选</span>'}</small></td>`,
      },
      {
        h: '行业',
        key: 'ind',
        cell: (r) => {
          const sr = secByName.get(r.ind);
          const tag = sr && sr.rank && sr.rank <= SECP.top ? `<span class="sec-rank" title="板块地位：涨≥${SECP.thresh}%家数排名">#${sr.rank}</span>` : '';
          return `<td class="ind" title="${esc(r.ind)}">${tag}${esc(r.ind) || '—'}</td>`;
        },
      },
      { h: '收盘', key: 'close', num: true, cell: (r) => `<td class="num">${fmt(r.close, 2)}</td>` },
      { h: '今日', key: 'chg', num: true, cell: (r) => pctCell(r.chg, 2) },
      { h: `RPS${s.short}`, key: 'rps' + s.short, num: true, cell: (r) => rpsCell(r['rps' + s.short], s.short_min) },
      { h: `RPS${s.long}`, key: 'rps' + s.long, num: true, cell: (r) => rpsCell(r['rps' + s.long], s.long_min) },
      { h: `${s.short}日`, key: 'r' + s.short, num: true, cell: (r) => pctCell(r['r' + s.short]) },
      { h: `${s.long}日`, key: 'r' + s.long, num: true, cell: (r) => pctCell(r['r' + s.long]) },
      { h: '距MA20', key: 'ma20d', num: true, title: '前复权收盘价相对20日均线', cell: (r) => pctCell(r.ma20d) },
      { h: '距10日高', key: 'pull10', num: true, title: '相对最近10根日K的最高收盘价', cell: (r) => pctCell(r.pull10) },
      { h: '距250日高', key: 'hi250d', num: true, cell: (r) => pctCell(r.hi250d) },
      { h: '流通市值(亿)', key: 'cap', num: true, cell: (r) => `<td class="num">${r.cap === null ? '—' : fmt(r.cap, 0)}</td>` },
      { h: '20日均额(亿)', key: 'amt20', num: true, cell: (r) => `<td class="num">${fmt(r.amt20, 2)}</td>` },
      { h: '连续入选', key: 'streak' + k, num: true, title: '连续出现在本参数组名单的快照天数（含今天）', cell: (r) => `<td class="num">${r['streak' + k] ? r['streak' + k] + '天' : '—'}</td>` },
      { h: '状态', key: 'state', cell: (r) => `<td>${r.state ? `<span class="table-chip ${chipClass[r.state] || ''}">${r.state}</span>` : '—'}</td>` },
    ];
  }

  function filtered() {
    const k = state.set;
    const q = state.q.trim().toLowerCase();
    let list = q ? rows.filter((r) => r.code.includes(q) || r.name.toLowerCase().includes(q)) : rows.filter((r) => r['in' + k]);
    if (state.ind !== 'all') list = list.filter((r) => (r.ind || '未分类') === state.ind);
    if (state.st === 'new') list = list.filter((r) => r['new' + k]);
    else if (state.st !== 'all') list = list.filter((r) => r.state === state.st);
    if (state.cap && !q) list = list.filter((r) => r.cap === null || r.cap >= D.meta.min_cap);
    if (state.watch) list = list.filter((r) => favs.has(r.code));
    const key = state.sort || 'rps' + cur().short;
    const dir = state.sort ? state.dir : -1;
    return list.slice().sort((a, b) => {
      const x = a[key];
      const y = b[key];
      if (x === y) return 0;
      if (x === null || x === undefined || x === '') return 1;
      if (y === null || y === undefined || y === '') return -1;
      return (x > y ? 1 : -1) * dir;
    });
  }

  function table() {
    const cols = columns();
    const activeKey = state.sort || 'rps' + cur().short;
    $('headRow').innerHTML = cols
      .map(
        (c) =>
          `<th ${c.key ? `data-sort="${c.key}"` : ''} class="${c.num ? 'num' : ''} ${c.key === activeKey ? 'sorted' : ''}" ${c.title ? `title="${c.title}"` : ''}>${c.h}${c.key === activeKey ? (state.sort && state.dir > 0 ? ' ↑' : ' ↓') : ''}</th>`,
      )
      .join('');
    const list = filtered();
    const shown = list.slice(0, state.limit);
    $('tableBody').innerHTML = shown.length
      ? shown.map((r) => `<tr>${cols.map((c) => c.cell(r)).join('')}</tr>`).join('')
      : `<tr><td colspan="${cols.length}" class="empty-note" style="padding:24px">没有符合条件的股票</td></tr>`;
    $('moreRows').hidden = list.length <= state.limit;
    $('moreRows').textContent = `显示更多（还有 ${list.length - shown.length} 只）`;
    const scope = state.q.trim() ? '搜索结果（含未入选）' : '入选明细';
    $('tableTitle').textContent = `${cur().label} · ${scope}`;
    $('resultCount').textContent = `共 ${list.length} 只${state.cap && !state.q.trim() ? ' · 已按流通市值≥30亿筛选' : ''}`;
  }

  function industries() {
    const list = D.industry[state.set] || [];
    $('industryBadge').textContent = `${list.length} 个行业 · ${D.meta.industry_date ? '分类更新于 ' + D.meta.industry_date : '同花顺行业'}`;
    const top = list.slice(0, 24);
    const max = Math.max(1, ...top.map((x) => x[1]));
    $('industryBars').innerHTML = top.length
      ? top
          .map(
            ([name, n, avg]) =>
              `<button type="button" class="ind-row ${state.ind === name ? 'active' : ''}" data-ind="${esc(name)}"><span title="${esc(name)}">${esc(name)}</span><div class="ind-track"><i style="width:${(n / max) * 100}%"></i></div><b>${n}</b><small>${fmt(avg, 1)}</small></button>`,
          )
          .join('')
      : '<p class="empty-note">没有入选股票</p>';
    const sel = $('industrySelect');
    const names = list.map((x) => x[0]);
    sel.innerHTML = '<option value="all">全部行业</option>' + names.map((n) => `<option value="${esc(n)}">${esc(n)}</option>`).join('');
    if (!names.includes(state.ind)) state.ind = 'all';
    sel.value = state.ind;
  }

  function method() {
    $('methodSets').textContent = KEYS.map((k) => {
      const s = SETS[k];
      return `${s.label}：RPS${s.short} > ${s.short_min} 且 RPS${s.long} > ${s.long_min}`;
    }).join('；') + '。两组用同一批股票、同一天的数据，页面顶部切换。';
    $('methodState').textContent = `只描述位置，不是买卖信号。跌破MA20：前复权收盘价低于20日均线；回踩中：在MA20上方，且距最近10根日K最高收盘价 ≤ ${D.meta.pullback}%；高位强势：其余在MA20上方的股票。“新”表示上一个快照不在本组名单里。`;
    $('methodSector').textContent = `每个交易日收盘后统计各同花顺行业中涨幅≥${SECP.thresh}%的股票家数，按家数排名，成分少于${SECP.min_members}只的行业不排名。家数看资金同时在做多少只，比平均涨幅更不容易被一两只涨停股拉偏；配合占比（剔除大盘普涨）、纯度、领涨（大票是否带头）和近${SECP.lookback}日持续性一起看。创业板、科创板涨跌幅20%，同一阈值下更容易入数。只用收盘数据，不是盘中信号。`;
    const log = D.log || [];
    $('healthSummary').textContent = `数据运行记录 · ${log.length} 条`;
    $('healthLog').textContent = log.join('\n');
  }

  // ── sector standing ─────────────────────────────────────────────────────
  const tagClass = { 主线: 'strong', 新晋: 'fresh', 退潮: 'broken' };
  function detail(r, picks, span) {
    const k = state.set;
    const chips = (r.stocks || [])
      .map(([code, name, chg, zt]) => {
        const row = byCode.get(code);
        const rps = row && row['in' + k] ? '<em class="rps-mark">RPS</em>' : '';
        return `<span class="chip up" data-code="${esc(code)}"><b>${esc(code)}</b>${esc(name)} <span class="positive">+${fmt(chg, 2)}%</span>${zt ? '<em class="zt-mark">涨停</em>' : ''}${rps}</span>`;
      })
      .join('');
    const btn = picks
      ? `<button type="button" class="sec-filter" data-filter="${esc(r.name)}">${state.ind === r.name ? '取消筛选' : `下方入选表只看本行业（${picks} 只）`}</button>`
      : '<span class="empty-note">本行业没有当前参数组的 RPS 入选股</span>';
    return `<tr class="sec-detail"><td colspan="${span}"><div class="sec-detail-inner"><p class="sec-detail-head">${esc(r.name)} · 今日涨≥${SECP.thresh}% 共 ${r.cnt5} 只（点名称在下方表格查看）</p><div class="chips">${
      chips || '<span class="empty-note">今天没有</span>'
    }</div>${btn}</div></td></tr>`;
  }

  function sectors() {
    const rowsS = SEC.rows;
    const n = SEC.dates.length;
    $('sectorBadge').textContent = n ? `${SEC.dates[0].slice(5)} ~ ${SEC.dates[n - 1].slice(5)} · ${rowsS.length} 个行业` : '无数据';
    $('sectorNote').textContent =
      `按当日收盘涨幅≥${SECP.thresh}%的家数排名（同花顺行业，每只股票只算一个行业）。占比＝占全市场涨≥${SECP.thresh}%家数；纯度＝占本行业有成交股票；` +
      `领涨＝成交额加权涨幅−平均涨幅，为正说明大票带头。近${SECP.lookback}日有${SECP.main_days}天以上排进前${SECP.top}记为主线，其中今天掉出前${SECP.top}的记为退潮。点击行业展开当日涨≥${SECP.thresh}%名单。`;
    const mx = Math.max(1, ...SEC.market);
    $('sectorMood').innerHTML = SEC.market
      .map(
        (v, i) =>
          `<div class="mood-day ${i === n - 1 ? 'today' : ''}"><b>${v}</b><div class="mood-track"><i style="height:${(v / mx) * 100}%"></i></div><span>${SEC.dates[i].slice(5)}</span></div>`,
      )
      .join('') + `<p class="mood-label">全市场涨≥${SECP.thresh}%<br />家数（情绪温度）</p>`;

    const k = state.set;
    const picks = new Map();
    rows.forEach((r) => r['in' + k] && picks.set(r.ind || '未分类', (picks.get(r.ind || '未分类') || 0) + 1));
    const heads = ['#', '行业', `涨≥${SECP.thresh}%`, '占比', '纯度', '涨停', '均涨幅', '加权', '领涨', `近${SECP.lookback}日家数`, `前${SECP.top}天数`, 'RPS入选'];
    $('sectorHead').innerHTML = heads.map((h, i) => `<th class="${i > 1 && i !== 9 ? 'num' : ''}">${h}</th>`).join('');
    const list = secAll ? rowsS : rowsS.filter((r) => r.rank && r.rank <= 15);
    const hmax = Math.max(1, ...rowsS.flatMap((r) => r.hist));
    $('sectorBody').innerHTML = list.length
      ? list
          .map((r) => {
            const prevRank = r.ranks.length > 1 ? r.ranks[r.ranks.length - 2] : null;
            let move = '';
            if (r.rank && prevRank) move = prevRank > r.rank ? `<i class="positive">↑${prevRank - r.rank}</i>` : prevRank < r.rank ? `<i class="negative">↓${r.rank - prevRank}</i>` : '';
            else if (r.rank && !prevRank) move = '<i class="positive">新</i>';
            const spark = r.hist
              .map((v, i) => `<i style="height:${Math.max(2, (v / hmax) * 100)}%" class="${r.ranks[i] && r.ranks[i] <= SECP.top ? 'hot' : ''}" title="${SEC.dates[i]}：${v} 只${r.ranks[i] ? ' · 第' + r.ranks[i] + '名' : ''}"></i>`)
              .join('');
            const p = picks.get(r.name) || 0;
            return `<tr class="${state.ind === r.name ? 'active' : ''}" data-ind="${esc(r.name)}" data-picks="${p}">
              <td class="num">${r.rank ?? '—'} ${move}</td>
              <td class="sec-name">${esc(r.name)}${r.tag ? ` <span class="table-chip ${tagClass[r.tag]}">${r.tag}</span>` : ''}<small>${r.n} 只</small></td>
              <td class="num"><b>${r.cnt5}</b></td>
              <td class="num">${fmt(r.share, 1)}%</td>
              <td class="num">${fmt(r.purity, 1)}%</td>
              <td class="num">${r.zt}</td>
              ${pctCell(r.avg, 2)}${pctCell(r.wavg, 2)}
              <td class="num ${r.lead > 0 ? 'positive' : r.lead < 0 ? 'negative' : ''}">${fmt(r.lead, 2, true)}</td>
              <td><div class="spark">${spark}</div></td>
              <td class="num">${r.top_days}/${n}</td>
              <td class="num">${p || '—'}</td></tr>${secOpen === r.name ? detail(r, p, heads.length) : ''}`;
          })
          .join('')
      : `<tr><td colspan="${heads.length}" class="empty-note" style="padding:24px">今天没有涨≥${SECP.thresh}%的股票</td></tr>`;
    const hidden = rowsS.length - list.length;
    $('sectorMore').hidden = !secAll && hidden <= 0;
    $('sectorMore').textContent = secAll ? '只看前 15' : `显示全部行业（还有 ${hidden} 个）`;
  }

  function renderAll() {
    setSwitch();
    kpis();
    changes();
    industries();
    sectors();
    table();
  }

  // ── events ──────────────────────────────────────────────────────────────
  $('searchInput').addEventListener('input', (e) => {
    state.q = e.target.value;
    state.limit = 300;
    table();
  });
  $('industrySelect').addEventListener('change', (e) => {
    state.ind = e.target.value;
    industries();
    sectors();
    table();
  });
  $('industryBars').addEventListener('click', (e) => {
    const b = e.target.closest('[data-ind]');
    if (!b) return;
    state.ind = state.ind === b.dataset.ind ? 'all' : b.dataset.ind;
    industries();
    sectors();
    table();
  });
  $('sectorBody').addEventListener('click', (e) => {
    const chip = e.target.closest('.chip[data-code]');
    if (chip) {
      // Show the stock in the main table (search covers stocks outside the selection).
      $('searchInput').value = chip.dataset.code;
      state.q = chip.dataset.code;
      table();
      document.querySelector('.table-section').scrollIntoView({ behavior: 'smooth' });
      return;
    }
    const f = e.target.closest('[data-filter]');
    if (f) {
      state.ind = state.ind === f.dataset.filter ? 'all' : f.dataset.filter;
      industries();
      sectors();
      table();
      if (state.ind !== 'all') document.querySelector('.table-section').scrollIntoView({ behavior: 'smooth' });
      return;
    }
    const tr = e.target.closest('tr[data-ind]');
    if (!tr) return;
    secOpen = secOpen === tr.dataset.ind ? null : tr.dataset.ind;
    sectors();
  });
  $('sectorMore').addEventListener('click', () => {
    secAll = !secAll;
    sectors();
  });
  $('stateFilter').addEventListener('click', (e) => {
    const b = e.target.closest('button[data-state]');
    if (!b) return;
    state.st = b.dataset.state;
    document.querySelectorAll('#stateFilter button').forEach((x) => x.classList.toggle('active', x === b));
    table();
  });
  $('capToggle').addEventListener('click', (e) => {
    state.cap = !state.cap;
    e.currentTarget.setAttribute('aria-pressed', String(state.cap));
    e.currentTarget.textContent = (state.cap ? '✓ ' : '') + '流通市值 ≥ 30亿';
    table();
  });
  $('watchOnly').addEventListener('click', (e) => {
    state.watch = !state.watch;
    e.currentTarget.setAttribute('aria-pressed', String(state.watch));
    e.currentTarget.textContent = (state.watch ? '★' : '☆') + ' 仅看自选';
    table();
  });
  $('resetFilters').addEventListener('click', () => {
    Object.assign(state, { q: '', ind: 'all', st: 'all', cap: true, watch: false, sort: null, limit: 300 });
    $('searchInput').value = '';
    $('capToggle').setAttribute('aria-pressed', 'true');
    $('capToggle').textContent = '✓ 流通市值 ≥ 30亿';
    $('watchOnly').setAttribute('aria-pressed', 'false');
    $('watchOnly').textContent = '☆ 仅看自选';
    document.querySelectorAll('#stateFilter button').forEach((x) => x.classList.toggle('active', x.dataset.state === 'all'));
    industries();
    table();
  });
  $('headRow').addEventListener('click', (e) => {
    const th = e.target.closest('th[data-sort]');
    if (!th) return;
    const key = th.dataset.sort;
    if (state.sort === key) state.dir = -state.dir;
    else {
      state.sort = key;
      state.dir = ['code', 'ind', 'state'].includes(key) ? 1 : -1;
    }
    table();
  });
  $('tableBody').addEventListener('click', (e) => {
    const b = e.target.closest('[data-fav]');
    if (!b) return;
    const c = b.dataset.fav;
    favs.has(c) ? favs.delete(c) : favs.add(c);
    store.set('favs', [...favs]);
    table();
  });
  $('moreRows').addEventListener('click', () => {
    state.limit += 300;
    table();
  });

  try {
    header();
    market();
    method();
    renderAll();
  } catch (err) {
    document.querySelector('main').insertAdjacentHTML('afterbegin', `<div class="stale-banner">页面渲染出错：${esc(err.message)}</div>`);
    throw err;
  }
  void byCode;
})();
