"""按画面变化挑帧，再拼成带时间标签的拼图，给模型直接读。

先降到每秒 2 帧再算 ffmpeg 的 scene 分。降采样后淡入淡出会变成一次跳变，
30fps 下它被摊到十几帧里，每帧分数都太低。挑帧有三种来源：
cut 是分数过阈值的切换（取变化结束后的那一帧），acc 是持续小变化累积够量，
gap 是太久没出帧的兜底。
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

from .ffmpeg_utils import find_ffmpeg

SAMPLE_FPS = 2


def sample(video: Path, work: Path) -> list[tuple[float, float, Path]]:
    ff = find_ffmpeg()
    if ff is None:
        raise RuntimeError("找不到 ffmpeg")
    raw = work / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    for p in raw.glob("*.jpg"):
        p.unlink()
    # metadata 的 file= 不能带盘符冒号，所以在 work 里用相对路径
    vf = (
        f"fps={SAMPLE_FPS},split[a][b];"
        "[a]scale=320:-2,select='gte(scene\\,0)',metadata=print:file=scores.txt,nullsink;"
        "[b]null[out]"
    )
    subprocess.run(
        [str(ff), "-hide_banner", "-loglevel", "error", "-i", str(video.resolve()),
         "-filter_complex", vf, "-map", "[out]", "-q:v", "3", "raw/r_%05d.jpg"],
        check=True, cwd=work,
    )
    text = (work / "scores.txt").read_text(encoding="utf-8", errors="replace")
    scores = [float(m) for m in re.findall(r"scene_score=([0-9.]+)", text)]
    frames = sorted(raw.glob("r_*.jpg"))
    n = min(len(scores), len(frames))
    return [(i / SAMPLE_FPS, scores[i], frames[i]) for i in range(n)]


def pick(
    scores: list[float],
    *,
    cut: float = 0.05,
    acc: float = 0.25,
    gap_samples: int = 40,
    max_frames: int = 80,
    merge_runs: bool = True,
) -> list[tuple[int, str]]:
    """返回 [(样本下标, 来源)]，来源是 first / cut / acc / gap。

    merge_runs 把连续的高分样本当成一次过渡，只留结束后那一帧。采样间隔
    很长时（B站 预览帧 5 到 10 秒一张）连续高分是几个不同画面，要关掉。
    """
    keep: list[tuple[int, str]] = [(0, "first")] if scores else []
    since = 0.0
    i = 1
    while i < len(scores):
        if scores[i] >= cut and not merge_runs:
            keep.append((i, "cut"))
            since = 0.0
            i += 1
            continue
        if scores[i] >= cut:
            j = i
            while j + 1 < len(scores) and scores[j + 1] >= cut:
                j += 1
            k = min(len(scores) - 1, j + 1)
            keep.append((k, "cut"))
            since = 0.0
            i = k + 1
            continue
        since += scores[i]
        if since >= acc:
            keep.append((i, "acc"))
            since = 0.0
        elif i - keep[-1][0] >= gap_samples:
            keep.append((i, "gap"))
            since = 0.0
        i += 1
    if max_frames <= 0 or len(keep) <= max_frames:
        return keep
    # ponytail: 超额时优先保留 cut，其余均匀抽稀；cut 本身超额就按位置均匀抽，不按分数排
    firm = [x for x in keep if x[1] in ("first", "cut")]
    soft = [x for x in keep if x[1] not in ("first", "cut")]
    if len(firm) >= max_frames:
        return _spread(firm, max_frames)
    return sorted(firm + _spread(soft, max_frames - len(firm)))


def _spread(items: list, k: int) -> list:
    n = len(items)
    if k <= 0:
        return []
    if n <= k:
        return list(items)
    if k == 1:
        return [items[n // 2]]
    return [items[round(i * (n - 1) / (k - 1))] for i in range(k)]


def _fmt(t: float) -> str:
    s = int(t)
    return f"{s // 60:02d}:{s % 60:02d}"


def make_sheets(picked: list[dict], out: Path, cols: int, rows: int, cell_w: int) -> None:
    from PIL import Image, ImageDraw, ImageFont

    try:
        font = ImageFont.truetype("arial.ttf", 22)
    except OSError:
        font = ImageFont.load_default()
    per = cols * rows
    for si in range(0, len(picked), per):
        chunk = picked[si:si + per]
        cells = []
        for fr in chunk:
            im = Image.open(fr["path"]).convert("RGB")
            cells.append(im.resize((cell_w, round(im.height * cell_w / im.width))))
        ch = cells[0].height
        used_rows = (len(cells) + cols - 1) // cols
        sheet = Image.new("RGB", (cols * cell_w + (cols - 1) * 4, used_rows * ch + (used_rows - 1) * 4), "white")
        d = ImageDraw.Draw(sheet)
        name = f"sheet_{si // per + 1:03d}.jpg"
        for k, (fr, im) in enumerate(zip(chunk, cells)):
            x, y = (k % cols) * (cell_w + 4), (k // cols) * (ch + 4)
            sheet.paste(im, (x, y))
            d.rectangle([x, y, x + 72, y + 28], fill="black")
            d.text((x + 6, y + 2), _fmt(fr["t"]), fill="yellow", font=font)
            fr["sheet"] = name
        sheet.save(out / name, quality=88)


def run(video: Path, session_dir: Path, *, max_frames: int, cols: int, rows: int, cell_w: int) -> Path:
    out = session_dir / "frames_change"
    out.mkdir(parents=True, exist_ok=True)
    for p in list(out.glob("sheet_*.jpg")) + list((out / "frames").glob("*.jpg")):
        p.unlink()
    samples = sample(video, out)
    chosen = pick([s for _, s, _ in samples], max_frames=max_frames)
    frames_dir = out / "frames"
    frames_dir.mkdir(exist_ok=True)
    picked = []
    for idx, kind in chosen:
        t, score, src = samples[idx]
        dst = frames_dir / f"{int(t) // 60:02d}m{int(t) % 60:02d}s_{idx:05d}.jpg"
        src.replace(dst)
        picked.append({"t": t, "kind": kind, "score": round(score, 4), "path": str(dst)})
    for p in (out / "raw").glob("*.jpg"):
        p.unlink()
    (out / "raw").rmdir()
    make_sheets(picked, out, cols, rows, cell_w)
    (out / "frames.json").write_text(json.dumps(picked, ensure_ascii=False, indent=1), encoding="utf-8")
    counts = {k: sum(1 for f in picked if f["kind"] == k) for k in ("first", "cut", "acc", "gap")}
    n_sheets = len(list(out.glob("sheet_*.jpg")))
    print(f"{len(samples)} 个采样点，挑出 {len(picked)} 帧 {counts}，拼成 {n_sheets} 张图")
    print(" ".join(_fmt(f["t"]) for f in picked))
    print(out)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="按画面变化挑帧并拼图")
    ap.add_argument("video", type=Path)
    ap.add_argument("session_dir", type=Path)
    ap.add_argument("--max", type=int, default=80, help="最多几帧（0 表示不限）")
    ap.add_argument("--grid", default="2x2", help="每张拼图几列几行，如 2x2、3x3")
    ap.add_argument("--cell-width", type=int, default=780)
    a = ap.parse_args(argv)
    cols, rows = (int(x) for x in a.grid.lower().split("x"))
    run(a.video, a.session_dir, max_frames=a.max, cols=cols, rows=rows, cell_w=a.cell_width)
    return 0


if __name__ == "__main__":
    sys.exit(main())
