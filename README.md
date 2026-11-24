# JADE RANK · A股 RPS 选股

A股全市场 RPS（相对价格强度）每日选股页面，界面与 [JADE PULSE](https://jade-a.pages.dev/) 同一风格。工作日北京时间 19:00 和 21:00 由 GitHub Actions 自动更新，发布到 Cloudflare Pages（默认项目名 `jade-rps`，网址 `https://jade-rps.pages.dev/`）。

## 选股规则

| 参数组 | 条件 |
|---|---|
| RPS 50/120 | RPS50 > 90 且 RPS120 > 85 |
| RPS 120/250 · 陶博士 | RPS120 > 90 且 RPS250 > 85 |

- **RPS_N** = N 日涨幅在全市场的百分位（0–100）。涨幅用前复权收盘价、按该股自己的 N 根日K计算；RPS250 需要至少 251 根日K。
- **参与排名**：沪深主板、创业板、科创板（不含北交所、B股），上市满 365 天，名称不含 ST/退，当日有成交。全市场一起排名，参数在 `rps.py` 的 `SETS`。
- **流通市值 ≥ 30 亿** 只是页面默认筛选，不影响 RPS 数值。流通市值来自集合竞价快照，只取入选股。
- **大盘风控**：沪深300 对比 MA20，只在页面顶部提示，名单照常列出。
- **状态标签**（描述位置，不是买卖信号）：跌破MA20；回踩中＝在 MA20 上方且距 10 日最高收盘价 ≤ −5%；高位强势＝其余。
- **每日变化**：与上一个快照对比的新进/退出、连续入选天数，记录在 `track/history.json`（由工作流提交）。

## 数据：每天只需十几次请求

全部来自同花顺金融 API，不用 baostock 逐只下载：

| 内容 | 接口 | 请求次数 |
|---|---|---|
| 全市场日K（未复权） | `market-dumps/daily-k-10d`，首次或断档超过增量窗口时用 `daily-k` 全量 | 1 |
| 除权除息事件 | `market-dumps/adjustment-factors`，本地前复权 | 1 |
| 代码表、上市日期 | `meta/tickers/list` | 1–2 |
| 沪深300 日线 | `a-share-index/prices/historical` | 1 |
| 入选股流通市值 | `a-share/auction/snapshot`（每次 100 只） | 约 5 |
| 同花顺行业 | `a-share-index` 行业指数及成分股，每周刷新一次并缓存 | 每周一次，约数百次 |

前复权：除权日参考前收盘 = (C − 每股分红 + 配股价 × 配股比例) / (1 + 送转比例 + 配股比例)，除权日之前的价格乘以“参考前收 / C”。本地保留最近 420 个交易日，存在 Actions 缓存里，不进 git。参与排名少于 3000 只时停止发布，避免百分位口径漂移。

## 首次配置

1. 仓库 **Settings → Secrets and variables → Actions** 添加两个 Repository secret（和 a-etf-dashboard 用同一组即可）：
   - `HITHINK_API_KEY`：同花顺金融 API key
   - `CLOUDFLARE_API_TOKEN`：有 Cloudflare Pages Edit 权限的 token
2. 可选 Repository variable：`CLOUDFLARE_PROJECT`（默认 `jade-rps`）、`CLOUDFLARE_ACCOUNT_ID`。
3. Actions 页面手动运行一次 **Publish RPS dashboard**。首次会下载全量日K（文件较大，几分钟），并自动创建 Cloudflare Pages 项目；之后每天 1–2 分钟。

运行失败时会开一个 Issue「[自动更新失败] RPS 选股每日发布」，成功后自动关闭。

## 本地运行

```sh
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
mkdir -p config && echo '你的key' > config/hithink_api_key.txt   # 或设置环境变量 HITHINK_API_KEY
python build.py              # 更新数据并生成 public/index.html
python build.py --render     # 用上次的快照重新生成页面，不联网
python build.py --synthetic  # 合成数据预览页面，不需要 key
python -m unittest discover -s tests -v
```

仅为历史数据统计，不构成投资建议。
