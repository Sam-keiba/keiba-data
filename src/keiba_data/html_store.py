"""取得した生HTMLのgzip保存。

確定したレース結果ページは後から変化しないので、パースに成功したものを
`{root}/{kind}/{年}/{key}.html.gz` に保存しておく。パーサを直したときに
`keiba reparse` でネットにアクセスせずDBを作り直すため、またDBを消してしまった
ときに再取得せず復元するために使う。
"""

from __future__ import annotations

import gzip
from collections.abc import Iterator
from pathlib import Path


class HtmlStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, kind: str, key: str) -> Path:
        return self.root / kind / key[:4] / f"{key}.html.gz"

    def get(self, kind: str, key: str) -> str | None:
        path = self._path(kind, key)
        if not path.exists():
            return None
        with gzip.open(path, "rt", encoding="utf-8") as f:
            return f.read()

    def put(self, kind: str, key: str, html: str) -> None:
        path = self._path(kind, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        with gzip.open(tmp, "wt", encoding="utf-8") as f:
            f.write(html)
        tmp.replace(path)

    def keys(self, kind: str) -> Iterator[str]:
        base = self.root / kind
        if not base.exists():
            return
        for path in sorted(base.glob("*/*.html.gz")):
            yield path.name.removesuffix(".html.gz")
