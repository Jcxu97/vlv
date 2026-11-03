from bilibili_vision.changeframes import pick


def test_pick_takes_frame_after_fade_and_caps_soft_picks():
    # 静止，两个样本的淡入淡出，再静止，之后一段持续小变化
    scores = [0.0, 0.0, 0.08, 0.12, 0.01, 0.0] + [0.03] * 60
    got = pick(scores, cut=0.05, acc=0.25, gap_samples=40, max_frames=0)
    assert got[0] == (0, "first")
    assert got[1] == (4, "cut")
    assert len(got) > 4
    assert all(kind == "acc" for _, kind in got[2:])

    capped = pick(scores, cut=0.05, acc=0.25, gap_samples=40, max_frames=4)
    assert len(capped) == 4
    assert (4, "cut") in capped
