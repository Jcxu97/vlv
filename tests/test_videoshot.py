"""videoshot: tile k maps to index[k+1], and pvdata replaces an empty index."""
from __future__ import annotations

import io
import struct
from pathlib import Path

from PIL import Image

from bilibili_vision import videoshot

COLORS = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0)]


def _sheet_bytes() -> bytes:
    im = Image.new("RGB", (40, 20))
    for k, c in enumerate(COLORS):
        im.paste(c, ((k % 2) * 20, (k // 2) * 10, (k % 2 + 1) * 20, (k // 2 + 1) * 10))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


class _Resp:
    def __init__(self, *, json_body=None, content=b""):
        self._json, self.content = json_body, content

    def raise_for_status(self):
        pass

    def json(self):
        return self._json


class _FakeSession:
    def __init__(self):
        self.headers, self.cookies = {}, {}

    def get(self, url, params=None, timeout=None):
        if url.endswith("/pagelist"):
            return _Resp(json_body={"code": 0, "data": [{"cid": 1}]})
        if url.endswith("/videoshot"):
            data = {"index": [], "pvdata": "//x/pv.bin", "image": ["//x/s.png"],
                    "img_x_len": 2, "img_y_len": 2, "img_x_size": 20, "img_y_size": 10}
            return _Resp(json_body={"code": 0, "data": data})
        if url.endswith("pv.bin"):
            return _Resp(content=struct.pack(">5H", 0, 0, 5, 10, 15))
        return _Resp(content=_sheet_bytes())


def test_fetch_maps_tiles_and_reads_pvdata(tmp_path: Path, monkeypatch):
    import requests

    monkeypatch.setattr(requests, "Session", _FakeSession)
    tl = videoshot.fetch_videoshots("https://www.bilibili.com/video/BV1FXHs68Eh1", tmp_path,
                                    cookies_path=tmp_path / "none.txt", max_frames=0)
    assert [e["t"] for e in tl] == [0, 5, 10, 15]
    for e, c in zip(tl, COLORS):
        px = Image.open(e["path"]).convert("RGB").getpixel((10, 5))
        assert all(abs(a - b) < 16 for a, b in zip(px, c))
