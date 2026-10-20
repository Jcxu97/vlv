"""用 B 站进度条预览拼图（videoshot）快速取帧，不下载视频。

一张拼图是 img_x_len × img_y_len 格，每格 480×270。第 k 格对应 index[k+1]，
index[0] 是占位（实测用感知哈希比对过）。间隔由 B 站决定，一般 5 到 10 秒一格。
"""
from __future__ import annotations

import argparse
import io
import json
import re
import struct
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .paths import PROJECT_ROOT

_API = "https://api.bilibili.com/x/player"
_HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.bilibili.com/"}
_BVID_RE = re.compile(r"BV[0-9A-Za-z]{10}")


def _load_cookies(path: Path) -> dict[str, str]:
    """读 Netscape 格式 cookies.txt；游客 buvid3 就够过风控。"""
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.split("\t")
        if len(parts) >= 7 and not line.startswith("#"):
            out[parts[5]] = parts[6]
    return out


def _get_json(session, url: str, params: dict) -> dict:
    r = session.get(url, params=params, timeout=20)
    r.raise_for_status()
    body = r.json()
    if body.get("code") != 0:
        raise RuntimeError(f"B 站接口返回 {body.get('code')}：{body.get('message')}（{url}）")
    return body["data"]


def _parse_target(url: str) -> tuple[str, int]:
    m = _BVID_RE.search(url)
    if not m:
        raise ValueError(f"链接里没有 BV 号：{url}")
    page = 1
    q = parse_qs(urlparse(url).query)
    if q.get("p", [""])[0].isdigit():
        page = int(q["p"][0])
    return m.group(0), page


def _fmt_t(sec: int) -> str:
    return f"{sec // 60:02d}m{sec % 60:02d}s"


def fetch_videoshots(
    url: str,
    out_dir: Path,
    *,
    cookies_path: Path | None = None,
    max_frames: int = 40,
) -> list[dict]:
    """取预览帧存到 out_dir/videoshot/，返回 [{"t": 秒, "path": str}]，并写 videoshot.json。"""
    import requests
    from PIL import Image

    from .extract_bilibili_text import COOKIES

    bvid, page = _parse_target(url)
    s = requests.Session()
    s.headers.update(_HEADERS)
    s.cookies.update(_load_cookies(cookies_path or COOKIES))

    pages = _get_json(s, f"{_API}/pagelist", {"bvid": bvid})
    if not 1 <= page <= len(pages):
        raise ValueError(f"{bvid} 只有 {len(pages)} P，没有第 {page} P")
    cid = pages[page - 1]["cid"]

    d = _get_json(s, f"{_API}/videoshot", {"bvid": bvid, "cid": cid, "index": 1})
    index = d.get("index") or []
    sheets = d.get("image") or []
    if not index and d.get("pvdata"):
        # index 时有时无（实测约一半请求为空）；pvdata 是同一组秒数，大端 uint16
        src = d["pvdata"]
        r = s.get(("https:" + src) if src.startswith("//") else src, timeout=20)
        r.raise_for_status()
        index = list(struct.unpack(f">{len(r.content) // 2}H", r.content[: len(r.content) // 2 * 2]))
    if len(index) < 2 or not sheets:
        raise RuntimeError(f"{bvid} 没有预览拼图，请改用 --vision 下载视频抽帧")
    cols, rows = int(d["img_x_len"]), int(d["img_y_len"])
    w, h = int(d["img_x_size"]), int(d["img_y_size"])

    times = index[1:]
    k_all = list(range(len(times)))
    if max_frames > 0 and len(k_all) > max_frames:
        k_all = sorted({round(i * (len(times) - 1) / (max_frames - 1)) for i in range(max_frames)})

    shot_dir = out_dir / "videoshot"
    shot_dir.mkdir(parents=True, exist_ok=True)
    for old in shot_dir.glob("*.jpg"):
        old.unlink()

    per_sheet = cols * rows
    cache: dict[int, Image.Image] = {}
    timeline: list[dict] = []
    seen: set[int] = set()
    for k in k_all:
        t = int(times[k])
        if t in seen:
            continue
        seen.add(t)
        si, pos = divmod(k, per_sheet)
        if si >= len(sheets):
            break
        if si not in cache:
            src = sheets[si]
            r = s.get(("https:" + src) if src.startswith("//") else src, timeout=30)
            r.raise_for_status()
            cache[si] = Image.open(io.BytesIO(r.content)).convert("RGB")
        x, y = (pos % cols) * w, (pos // cols) * h
        p = shot_dir / f"{_fmt_t(t)}.jpg"
        cache[si].crop((x, y, x + w, y + h)).save(p, quality=90)
        timeline.append({"t": t, "path": str(p.resolve())})

    (out_dir / "videoshot.json").write_text(
        json.dumps({"bvid": bvid, "page": page, "timeline": timeline}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return timeline


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="用 B 站预览拼图取帧，不下载视频")
    ap.add_argument("url", help="B 站视频链接或 BV 号（?p=N 选分 P）")
    ap.add_argument("--out", type=Path, default=PROJECT_ROOT / "out", help="会话目录（vlv extract 的输出目录）")
    ap.add_argument("--max", type=int, default=40, help="最多取几帧，在全片均匀取（0 表示全取）")
    args = ap.parse_args(argv)
    try:
        tl = fetch_videoshots(args.url, args.out, max_frames=args.max)
    except (RuntimeError, ValueError) as e:
        print(str(e), file=sys.stderr)
        return 1
    print(f"取到 {len(tl)} 帧：{args.out / 'videoshot'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
