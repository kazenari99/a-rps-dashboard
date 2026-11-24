"""Tonghuashun (同花顺) Financial API client: JSON endpoints plus Parquet market dumps.

The key only travels in the X-api-key header from this process. Presigned dump URLs
are short-lived credentials: they are used in-process and never logged or stored.
"""

from __future__ import annotations

import json
import os
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = 'https://fuyao.aicubes.cn'
USER_AGENT = 'A-RPS-Dashboard/1.0'
ROOT = Path(__file__).resolve().parent
# Business codes that mean the credential was rejected (JSON endpoints use 2001/2003,
# the dump endpoints document 2002/2004).
AUTH_CODES = (2001, 2002, 2003, 2004)
RETRY_CODES = (4001, 5001, 5002, 5003)
NOT_READY = 4040
PAUSE = 0.4  # seconds between JSON calls; the API applies dynamic rate limiting


class APIError(RuntimeError):
    def __init__(self, code, request_id=None, path=''):
        self.code = code
        super().__init__(f'同花顺业务错误 {code}（{path}）；request_id={request_id or "未提供"}')


class AuthError(RuntimeError):
    """Credential rejected: abort the whole run instead of degrading to cache."""


def api_key():
    value = os.environ.get('HITHINK_API_KEY', '').strip()
    path = ROOT / 'config/hithink_api_key.txt'
    if not value and path.exists():
        value = path.read_text().strip()
    if not value:
        raise RuntimeError('缺少 HITHINK_API_KEY；请设置环境变量、config/hithink_api_key.txt 或 GitHub Actions Secret。')
    return value


class Client:
    def __init__(self, key=None, pause=PAUSE):
        self.key = key or api_key()
        self.pause = pause
        self.calls = 0
        self._last = 0.0

    def _throttle(self):
        wait = self.pause - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()

    def get(self, path, params=None, attempts=4):
        """GET JSON with bounded exponential backoff; auth failures abort immediately."""
        url = BASE + path + ('?' + urllib.parse.urlencode(params) if params else '')
        req = urllib.request.Request(url, headers={'X-api-key': self.key, 'User-Agent': USER_AGENT})
        for attempt in range(attempts):
            last = attempt == attempts - 1
            self._throttle()
            self.calls += 1
            try:
                with urllib.request.urlopen(req, timeout=30) as response:
                    payload = json.load(response)
                code = payload.get('code')
                if code != 0:
                    if code in AUTH_CODES:
                        raise AuthError(f'同花顺认证失败（业务码 {code}）；请检查 HITHINK_API_KEY。')
                    raise APIError(code, payload.get('request_id'), path)
                return payload.get('data') or {}
            except APIError as exc:
                if exc.code not in RETRY_CODES or last:
                    raise
            except urllib.error.HTTPError as exc:
                if exc.code in (401, 403):
                    raise AuthError(f'同花顺认证失败 HTTP {exc.code}；请检查 HITHINK_API_KEY。') from None
                if exc.code not in (429, 500, 502, 503, 504) or last:
                    raise RuntimeError(f'HTTP {exc.code}（{path}）') from None
            except json.JSONDecodeError:
                if last:
                    raise RuntimeError(f'同花顺返回内容无法解析（{path}）') from None
            except (urllib.error.URLError, TimeoutError):
                if last:
                    raise RuntimeError(f'同花顺连接超时或网络不可用（{path}）') from None
            time.sleep(2**attempt + random.uniform(0, 0.5))
        raise RuntimeError(f'同花顺请求失败（{path}）')

    def download_dump(self, kind, dest, attempts=3):
        """Sign and immediately download a market dump Parquet file (`daily-k`,
        `daily-k-10d` or `adjustment-factors`). Returns the destination path."""
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + '.part')
        for attempt in range(attempts):
            last = attempt == attempts - 1
            try:
                data = self.get(f'/api/dump/market-dumps/{kind}/download-url')
            except APIError as exc:
                if exc.code == NOT_READY and not last:
                    time.sleep(60)
                    continue
                raise
            url = data.get('presigned_url')
            if not url:
                raise RuntimeError(f'{kind} 未返回下载链接')
            try:
                req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
                with urllib.request.urlopen(req, timeout=600) as response, open(tmp, 'wb') as fh:
                    while chunk := response.read(1 << 20):
                        fh.write(chunk)
                tmp.replace(dest)
                return dest
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                # An expired link answers 403: sign again instead of reusing it.
                status = getattr(exc, 'code', None)
                if last:
                    raise RuntimeError(f'{kind} 下载失败（HTTP {status or "网络错误"}）') from None
                time.sleep(5 * (attempt + 1))
            finally:
                del url
        raise RuntimeError(f'{kind} 下载失败')

    def paged(self, path, params, limit, key='item'):
        out, offset = [], 0
        while True:
            data = self.get(path, {**params, 'limit': limit, 'offset': offset})
            items = data.get(key) or []
            out.extend(items)
            if len(items) < limit:
                return out
            offset += limit
