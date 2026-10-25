#!/usr/bin/env python3
"""script_lint.py —— 动手 build 之前，机械检查配音分段与板书排期。

移植自花叔 huashu-art-motion 的 storyboard_lint 机制（红灯不放行、每条带修法），
检查对象改为本技能的两份前置产物：

用法：
  python script_lint.py narration.json                     # 只查配音分段（Stage A）
  python script_lint.py narration.json project.json        # 加查排期与声画绑定（Stage B）
  python script_lint.py project.json narration.json        # 顺序无所谓，按内容自动识别
  可选：--lead-tol 320 --finish-tol 220 --tail-min 300 --max-pause 700

自动识别：含 segments 键的是 narration.json；含 scenes 键的是 project.json。

Stage A（只要 narration.json 就查）：
  A1 句长：单句 > 8s → 红（v2 声画漂移的根因：长句撑多元素、中间没锚点）；
           5–8s → 黄（建议拆句，一句一个画面动作）
  A2 多音字：句中出现风险字（行/当/得/重/传/落/曲/率…）→ 黄，
             修法：换等义词规避，或录完后跑 polyphone_check.py 声学核验
  A3 开场抽象：第一句含「是指/所谓/定义/叫做/是一种」→ 红（概念解释反模式：
               开场必须先给具体场景的锚，术语后置）
  A4 术语负债：同一板内引号术语（「」/""）超过 2 个 → 黄（一板至多引入 1 个新术语）
  A5 停顿：相邻句间隔 > maxPause → 黄；> 2.5s → 红（观众断线）

Stage B（需要 project.json，绝对轴 = scene.startMs + 元素相对 startMs）：
  B1 绑定完整性：配音段没有任何元素绑定（drawGroup = "g"+段id）→ 黄（这句口播没有画面动作）
  B2 同步判据：画面领先句首超过 --lead-tol → 红；画完晚于句末超过 --finish-tol → 红
  B3 长句多元素：单句 > 5s 且绑定元素 ≥ 5 → 黄（提醒拆句或在句内分锚点）
  B4 drawGroup 连续性：同组元素的跨度里夹了别组的元素 → 红（schedule 会算错 scope，
     整板被压缩——v2 第四板「16」没画出来的根因）
  B5 帧边界：durationMs × fps 不能被 1000 整除 → 红（render 直接报错）
  B6 末板收尾：最后一场 endMs ≠ durationMs → 红；任意一场最后一个元素画完到
     场景末尾不足 --tail-min → 黄（完整态没停留，观众看不清成品）
  B7 首元素：场景内第一个元素 startMs = 0 → 黄（约定给 40ms，避开 all-drawn 首帧判定）

退出码：有红灯 1；只有黄灯或全绿 0；读不了文件 2。只用标准库。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

RED, YELLOW, GREEN = "红", "黄", "绿"

# A2 多音字风险字（Edge TTS 常见翻车字；不在表内 ≠ 一定安全）
POLYPHONE = "行当得重传落曲率弹调干缝假供应发卡分省处兴屏教系奇数长还指相空率"
# A3 开场抽象词
ABSTRACT_OPEN = re.compile(r"是指|所谓|定义|叫做|是一种|的概念|这个概念")
# A4 引号术语
QUOTED = re.compile(r"[「『][^」』]{1,12}[」』]|[\"“][^\"”]{1,12}[\"”]")


def load(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        print(f"读不了文件：{path}")
        sys.exit(2)


def classify(a, b):
    """按内容识别哪份是 narration、哪份是 project。"""
    docs = [load(a), load(b)]
    narr = next((d for d in docs if isinstance(d, dict) and "segments" in d), None)
    proj = next((d for d in docs if isinstance(d, dict) and "scenes" in d), None)
    return narr, proj


def seg_board(seg_id: str) -> str:
    m = re.match(r"(\d+)", str(seg_id))
    return m.group(1) if m else str(seg_id)


def lint_narration(narr, max_pause, issues):
    segs = narr.get("segments") or []
    if not segs:
        issues.append((RED, "A0", "narration.json 里没有 segments", "检查 make_sync.py 输出"))
        return
    # A1 / A2 / A3 逐句
    for s in segs:
        dur = s.get("endMs", 0) - s.get("startMs", 0)
        text = s.get("text", "")
        if dur > 8000:
            issues.append((RED, "A1句长", f"段 {s['id']} 一句 {dur/1000:.1f}s，超过 8s",
                           "拆成两句：一句一个画面动作，长句撑多元素没有锚点，必然声画漂移"))
        elif dur > 5000:
            issues.append((YELLOW, "A1句长", f"段 {s['id']} 一句 {dur/1000:.1f}s（5–8s）",
                           "能拆就拆；不拆就保证这句只画一个连续动作"))
        hits = sorted({ch for ch in text if ch in POLYPHONE})
        if hits:
            issues.append((YELLOW, "A2多音字", f"段 {s['id']} 含风险字：{' '.join(hits)}",
                           "换等义词规避（行→排、当→作为），或录完跑 polyphone_check.py 核验读音"))
    first = segs[0].get("text", "")
    if ABSTRACT_OPEN.search(first):
        issues.append((RED, "A3开场", "第一句就是抽象定义（是指/所谓/…）",
                       "开场先给具体场景的锚（一个物在做一件具体的事），术语后置到落差之后再给"))
    # A4 术语负债（按板聚合）
    boards: dict = {}
    for s in segs:
        terms = {m.group(0).strip("「」『』\"\"\"") for m in QUOTED.finditer(s.get("text", ""))}
        boards.setdefault(seg_board(s["id"]), set()).update(t for t in terms if len(t) >= 2)
    for b, terms in sorted(boards.items()):
        if len(terms) > 2:
            issues.append((YELLOW, "A4术语", f"第 {b} 板引入 {len(terms)} 个术语：{'、'.join(sorted(terms))}",
                           "一板至多引入 1 个新术语，其余挪到后面的板"))
    # A5 停顿
    for a, b in zip(segs, segs[1:]):
        gap = b["startMs"] - a["endMs"]
        if gap > 2500:
            issues.append((RED, "A5停顿", f"段 {a['id']} → {b['id']} 间隔 {gap/1000:.1f}s",
                           "口播断档超过 2.5s 观众会断线；补一句串场或压缩间隙"))
        elif gap > max_pause:
            issues.append((YELLOW, "A5停顿", f"段 {a['id']} → {b['id']} 间隔 {gap}ms > maxPause {max_pause}ms",
                           "板间停顿控制在 maxPause 内；收尾留白从场景时长里出，别靠静音撑"))


def lint_project(narr, proj, lead_tol, finish_tol, tail_min, issues):
    segs = {s["id"]: s for s in (narr or {}).get("segments", [])}
    scenes = proj.get("scenes") or []
    fps = proj.get("fps", 30)
    duration = proj.get("durationMs", 0)

    # B5 帧边界
    if duration and (duration * fps) % 1000:
        issues.append((RED, "B5帧边界", f"durationMs {duration} × {fps}fps 不落在帧边界",
                       "durationMs 向上取整到 100ms 的整数倍（ceil(x/100)*100）"))

    # B6 末板
    if scenes:
        last = scenes[-1]
        if last.get("endMs") != duration:
            issues.append((RED, "B6末板", f"最后一场 endMs {last.get('endMs')} ≠ durationMs {duration}",
                           "对齐帧边界后把末场 endMs 精确设为 durationMs"))
    for sc in scenes:
        els = [e for e in sc.get("elements", []) if isinstance(e.get("startMs"), (int, float))]
        if not els:
            issues.append((YELLOW, "B1绑定", f"场景 {sc.get('id')} 没有任何元素", "删掉这场或补板书"))
            continue
        els.sort(key=lambda e: e["startMs"])
        abs0 = sc["startMs"]
        # B6 每场收尾留白
        last_end = abs0 + max(e["startMs"] + (e.get("durationMs") or e.get("minDurationMs") or 0) for e in els)
        tail = sc["endMs"] - last_end
        if tail < 0:
            issues.append((RED, "B6收尾", f"场景 {sc.get('id')} 末元素画到场景边界外 {tail}ms",
                           "元素窗口超出场景时长会被 validate 拒收（animation exceeds scene duration）；前移末元素或加长场景"))
        elif tail < tail_min:
            issues.append((YELLOW, "B6收尾", f"场景 {sc.get('id')} 收尾只留 {tail}ms（< {tail_min}ms）",
                           "schedule 会铺满场景，主动把末元素前移，留 300–1000ms 让完整态停住"))
        # B7 首元素
        if els[0]["startMs"] == 0:
            issues.append((YELLOW, "B7首元素", f"场景 {sc.get('id')} 首元素 startMs=0",
                           "给 40ms，避开 all-drawn 模式首帧非空的判定"))
        # B4 drawGroup 连续性
        groups: dict = {}
        for i, e in enumerate(els):
            groups.setdefault(e.get("drawGroup") or f"__anon{i}", []).append(i)
        for g, idxs in groups.items():
            if g.startswith("__anon"):
                continue
            span = (els[idxs[0]]["startMs"], els[idxs[-1]]["startMs"])
            intruders = sorted({els[i].get("drawGroup") for i in range(len(els))
                                if span[0] < els[i]["startMs"] < span[1]
                                and els[i].get("drawGroup") != g})
            if intruders:
                issues.append((RED, "B4组连续", f"场景 {sc.get('id')} 组 {g} 的跨度里夹着组 {'、'.join(sorted(intruders))}",
                               "drawGroup 必须对应连续时间块：把夹在中间的元素改名独立组或调序，否则 schedule 整板压缩"))
        # B1/B2/B3 与配音段绑定（约定 drawGroup = "g"+段id）
        bound: dict = {}
        for e in els:
            g = e.get("drawGroup") or ""
            if g.startswith("g") and g[1:] in segs:
                bound.setdefault(g[1:], []).append(e)
        scene_board = re.search(r"(\d+)", str(sc.get("id", "")))
        scene_board = scene_board.group(1) if scene_board else None
        for sid, seg in segs.items():
            if scene_board is None or seg_board(sid) != scene_board:
                continue  # 只对本场的段做判据检查
            els_b = bound.get(sid)
            if els_b is None:
                continue
            if not els_b:
                issues.append((YELLOW, "B1绑定", f"段 {sid}「{seg.get('text','')[:18]}…」没有任何元素绑定",
                               "这句口播在干讲；补一个画面动作或并进相邻句"))
                continue
            if len(els_b) >= 5 and (seg["endMs"] - seg["startMs"]) > 5000:
                issues.append((YELLOW, "B3长句多元素", f"段 {sid} {len(els_b)} 个元素撑 {(seg['endMs']-seg['startMs'])/1000:.1f}s",
                               "拆句，或至少在句中段各设一个可见锚点（v2 漂移的形态）"))
            for e in els_b:
                abs_start = abs0 + e["startMs"]
                abs_end = abs_start + (e.get("durationMs") or e.get("minDurationMs") or 0)
                lead = seg["startMs"] - abs_start          # >0 = 画面领先
                finish = abs_end - seg["endMs"]            # >0 = 画得比说得晚
                if lead > lead_tol:
                    issues.append((RED, "B2领先", f"段 {sid} 的 {e['id']} 领先口播 {lead}ms（> {lead_tol}）",
                                   "把元素 startMs 后移，让下笔落在句首 ±320ms 内"))
                if finish > finish_tol:
                    issues.append((RED, "B2拖拍", f"段 {sid} 的 {e['id']} 画完比句末晚 {finish}ms（> {finish_tol}）",
                                   "压缩 durationMs 或提前 startMs；宁可少画不拖拍"))


def main():
    ap = argparse.ArgumentParser(description="白板片开工前红灯门禁（移植自 storyboard_lint 机制）")
    ap.add_argument("inputs", nargs="+", help="narration.json 和/或 project.json")
    ap.add_argument("--lead-tol", type=int, default=320)
    ap.add_argument("--finish-tol", type=int, default=220)
    ap.add_argument("--tail-min", type=int, default=300)
    ap.add_argument("--max-pause", type=int, default=None, help="缺省取 narrationPolicy.maxPauseMs，否则 700")
    args = ap.parse_args()
    if len(args.inputs) not in (1, 2):
        print("给 1–2 个文件：narration.json / project.json")
        sys.exit(2)

    narr, proj = (classify(*args.inputs) + (None,))[:2] if len(args.inputs) == 2 else (classify(args.inputs[0], args.inputs[0]))
    if narr is None and proj is None:
        print("两个文件都识别不了（需要含 segments 或 scenes 键）")
        sys.exit(2)

    max_pause = args.max_pause or 700
    if narr is None:
        max_pause = ((proj.get("narrationPolicy") or {}).get("maxPauseMs") or 700)
    issues: list = []
    if narr:
        lint_narration(narr, max_pause, issues)
        if proj:
            lint_project(narr, proj, args.lead_tol, args.finish_tol, args.tail_min, issues)

    reds = [i for i in issues if i[0] == RED]
    yellows = [i for i in issues if i[0] == YELLOW]
    n_seg = len((narr or {}).get("segments", []))
    n_sc = len((proj or {}).get("scenes", []))
    print(f"检查范围：{'配音 ' + str(n_seg) + ' 句' if narr else ''}"
          f"{' + 排期 ' + str(n_sc) + ' 场' if proj else ''}")
    for sev, tag, what, fix in issues:
        mark = "🔴 红" if sev == RED else "🟡 黄"
        print(f"{mark} [{tag}] {what}\n        修法：{fix}")
    if not issues:
        print("🟢 绿：全部通过")
    print(f"\n结论：红灯 {len(reds)} ｜ 黄灯 {len(yellows)}"
          + ("  → 有红灯不放行，修完重跑" if reds else "  → 可以进 build"))
    sys.exit(1 if reds else 0)


if __name__ == "__main__":
    main()
