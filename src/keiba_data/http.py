"""サーバー負荷に配慮したHTTPアクセス用のセッションラッパー。"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

import requests

from keiba_data import config

logger = logging.getLogger(__name__)


class BlockedError(Exception):
    """400/403/429が連続した。IP単位のアクセスブロックを受けた疑いがある。"""


class RequestBudgetExceeded(Exception):
    """1回の実行で許可したリクエスト数の上限に達した。"""


@dataclass
class FetchRecord:
    """1回のHTTPアクセスの記録（fetch_logテーブル用）。"""

    url: str
    status_code: int | None
    ok: bool
    message: str | None = None


class PoliteSession:
    """requests.Sessionをラップし、アクセス間隔の制御・リトライ・ブロック検知・
    リクエスト数上限をまとめて行う。
    """

    def __init__(
        self,
        interval_sec: float = config.REQUEST_INTERVAL_SEC,
        max_requests: int | None = None,
        *,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        on_fetch: Callable[[FetchRecord], None] | None = None,
    ) -> None:
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": config.HTTP_USER_AGENT})
        self.interval_sec = interval_sec
        self.max_requests = max_requests
        self.n_requests = 0
        self._sleep = sleep
        self._clock = clock
        self._on_fetch = on_fetch
        self._last_request_at: float | None = None
        self._consecutive_block_responses = 0

    def _wait_for_interval(self) -> None:
        if self._last_request_at is None:
            return
        remaining = self.interval_sec - (self._clock() - self._last_request_at)
        if remaining > 0:
            self._sleep(remaining)

    def _record(self, url: str, status_code: int | None, ok: bool, message: str | None = None) -> None:
        if self._on_fetch is not None:
            self._on_fetch(FetchRecord(url=url, status_code=status_code, ok=ok, message=message))

    def get_bytes(self, url: str) -> bytes | None:
        """PDFなどのバイナリを取得する。404の場合はNoneを返す。"""
        response = self._request(url)
        return response.content if response is not None else None

    def get_text(self, url: str, *, encoding: str) -> str | None:
        """GETして本文を文字列で返す。404の場合はNoneを返す。"""
        response = self._request(url)
        if response is None:
            return None
        response.encoding = encoding
        return response.text

    def post_text(self, url: str, data: dict[str, str | bytes], *, encoding: str) -> str | None:
        """POSTして本文を文字列で返す。404の場合はNoneを返す。

        JRAの結果ページ（`/JRADB/accessS.html`）はGETでは開けず、`cname` をPOSTして辿る。
        待ち時間・リトライ・ブロック検知はGETと同じ仕組みを通す。

        値を **bytes で渡すとそのままURLエンコードされる**。競走馬検索は `cname` に
        日本語の馬名が入り、JRAはcp932で受け取るので、呼ぶ側でcp932に変えて渡す
        （文字列のまま渡すとrequestsがUTF-8で組んでしまい「パラメータエラー」になる）。
        """
        response = self._request(url, data=data)
        if response is None:
            return None
        response.encoding = encoding
        return response.text

    def _request(self, url: str, data: dict[str, str | bytes] | None = None) -> requests.Response | None:
        """GET（`data` を渡したときはPOST）を実行してレスポンスを返す。404の場合はNone。

        - タイムアウト・接続エラー・5xx: バックオフしながら最大MAX_RETRIES回まで試行し、失敗したら送出
        - 400/403/429: 連続回数がMAX_CONSECUTIVE_BLOCK_RESPONSESに達したら BlockedError
        - リクエスト数がmax_requestsに達していたら、アクセスせずに RequestBudgetExceeded
        """
        last_exc: Exception | None = None
        for attempt in range(1, config.MAX_RETRIES + 1):
            if self.max_requests is not None and self.n_requests >= self.max_requests:
                raise RequestBudgetExceeded(f"リクエスト数の上限({self.max_requests})に達しました")
            self._wait_for_interval()
            self._last_request_at = self._clock()
            self.n_requests += 1
            try:
                if data is None:
                    response = self.session.get(url, timeout=config.REQUEST_TIMEOUT_SEC)
                else:
                    response = self.session.post(url, data=data, timeout=config.REQUEST_TIMEOUT_SEC)
            except requests.RequestException as exc:
                last_exc = exc
                self._record(url, None, False, f"{type(exc).__name__}: {exc}")
                logger.warning("通信エラー (試行%d/%d): %s: %s", attempt, config.MAX_RETRIES, url, exc)
            else:
                status = response.status_code
                if status in config.BLOCK_STATUS_CODES:
                    self._consecutive_block_responses += 1
                    self._record(url, status, False, "blocked?")
                    logger.warning(
                        "HTTP %d (連続%d回目): %s", status, self._consecutive_block_responses, url
                    )
                    if self._consecutive_block_responses >= config.MAX_CONSECUTIVE_BLOCK_RESPONSES:
                        raise BlockedError(
                            f"HTTP {status} が{self._consecutive_block_responses}回連続しました。"
                            "アクセスをブロックされている可能性があります。"
                            "処理を中止します。数時間〜1日空けてから再実行してください。"
                        )
                    last_exc = requests.HTTPError(f"HTTP {status}: {url}", response=response)
                else:
                    self._consecutive_block_responses = 0
                    if status == 404:
                        self._record(url, status, False, "not found")
                        return None
                    if status >= 500:
                        last_exc = requests.HTTPError(f"HTTP {status}: {url}", response=response)
                        self._record(url, status, False, "server error")
                        logger.warning("HTTP %d (試行%d/%d): %s", status, attempt, config.MAX_RETRIES, url)
                    elif status >= 400:
                        self._record(url, status, False)
                        response.raise_for_status()
                    else:
                        self._record(url, status, True)
                        return response
            if attempt < config.MAX_RETRIES:
                self._sleep(5.0 * 2 ** (attempt - 1))  # 5秒, 10秒

        assert last_exc is not None
        raise last_exc
