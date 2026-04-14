"""
本地 Gemma（OpenAI 兼容）对单帧「一句话」画面描述：供 vision_deep_pipeline 稀疏调用。
"""
from __future__ import annotations

# 与产品约定一致：短输出、降 token / 显存峰值；无信息时固定用语便于下游丢弃
FRAME_VISION_PROMPT_ZH = (
    "请用一句话（不超过 60 字）只描述画面中明确可见的内容（人物、界面、文字大意、图表类型等）。"
    "不要推测原因、剧情或屏幕外信息。"
    "若画面模糊、无实质信息或与黑屏/过场类似，请只回复：无关键信息"
)

# 单帧补全上限（略收紧，减少胡编长句）
FRAME_VISION_MAX_TOKENS = 96


def should_drop_frame_caption(text: str) -> bool:
    """过滤低信息量或与 OCR 易重复的废话描述。"""
    t = (text or "").strip()
    if len(t) < 6:
        return True
    low = t.lower()
    drops = (
        "无关键信息",
        "没有关键信息",
        "无可描述",
        "无明显信息",
        "无可见信息",
        "看不清",
        "无法识别",
        "画面模糊",
        "黑屏",
        "纯色",
        "无实质",
        "没有明显",
        "暂无",
    )
    if any(p in t for p in drops):
        return True
    # 泛化套话（信息密度极低）
    if low in ("无。", "无", "没有。", "没有", "none.", "none", "n/a"):
        return True
    return False
