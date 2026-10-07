"""定额预套：规则优先 + AI 兜底（2026-10-05）。

用户需求：内置了 AI，能不能先**预套一次**（按产品分类 + 定额名称匹配 ✓），
人工确认无误后就不用再一条条选了 ✓。

设计原则（防止"AI 编一个定额出来" ✗）：
  ① **规则先跑**：按产品分类定位定额章（视频监控→5-6 等 ✓），
     再按名称的**字符二元组重合度**给候选打分排序 ✓
  ② **AI 只在候选里挑**：把该行的名称/型号/分类 + Top-5 候选交给模型 ✓，
     只许输出候选里的编号，或回答"不确定" ✓ —— 从机制上不可能编造 ✓
  ③ 低置信度的**不自动套** ✗，留给人选 ✓；本接口**只给建议、不写库** ✓
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.labor import QuotaItem
from app.models.quote import Quote
from app.models.quote_item import QuoteItem

# 产品/分类关键词 → 定额章（2025 版第五册 ✓）
CHAPTER_HINTS: list[tuple[str, str]] = [
    (r"摄像|监控|球机|枪机|录像|硬盘录像|门禁|入侵|报警|对讲|巡更|周界"
     r"|安防", "5-6"),
    (r"机柜|配线架|跳线|信息插座|插座|线缆|双绞|光缆|光纤|尾纤|桥架|线槽"
     r"|穿放|敷设|模块|耦合器", "5-2"),
    (r"交换机|路由器|防火墙|服务器|网络|KVM|打印机|存储|网桥|无线|AP|光模块", "5-1"),
    (r"传感器|变送器|DDC|楼控|自控|BAS|流量计|液位|压差|温湿度|风速", "5-3"),
    (r"广播|扬声器|功放|音柱|背景音乐|分区", "5-5"),
    (r"电视|卫星|调制器|混合器|放大器|前端|射频", "5-4"),
    (r"电涌|防雷|SPD|避雷|接地", "5-7"),
    (r"家居|可视对讲|智能控制|住宅", "5-8"),
    (r"时钟|母钟|子钟|GPS|北斗", "5-9"),
    (r"会议|投影|显示屏|拼接|话筒|麦克风|扩声|音视频", "5-5"),
]
STOP = set("的与及和式装安装调试试安装工程系统设备材料台套个只条米米以内以下以上每增加")


def guess_chapter(text: str) -> str | None:
    for pat, cat in CHAPTER_HINTS:
        if re.search(pat, text, re.I):
            return cat
    return None


def toks(s: str) -> set[str]:
    """字符二元组 + 英文/数字词（中文短名靠二元组重合度就够用 ✓）。"""
    s = re.sub(r"[（）()\[\]【】,，、:：;；\s]+", "", str(s or "")).lower()
    out = {s[i:i + 2] for i in range(len(s) - 1)}
    out |= {w.lower() for w in re.findall(r"[a-z0-9]{2,}", str(s or "").lower())}
    return out


def overlap(a: str, b: str) -> float:
    ta, tb = toks(a), toks(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


async def suggest_for_quote(db: AsyncSession, quote: Quote,
                            *, top_n: int = 5) -> dict[str, Any]:
    items = list((await db.scalars(
        select(QuoteItem).where(QuoteItem.quote_id == quote.id)
    )).all())
    pool = list((await db.scalars(
        select(QuotaItem).where(QuotaItem.is_active.is_(True))
    )).all())

    rows: list[dict[str, Any]] = []
    for it in items:
        notes = str(it.notes or "")
        if "quota:" in notes:            # 已套定额的行跳过 ✓
            continue
        text = " ".join(str(x or "") for x in (it.name, it.model, it.brand))
        chapter = guess_chapter(text)
        cands = [q for q in pool if chapter is None or
                 str(getattr(q.category, "value", q.category)) == chapter]
        if not cands:
            cands = pool
        scored = sorted(
            ({"code": q.code, "name": q.name, "unit": q.unit,
              "labor_b_day": str(q.labor_b_day), "base_price": str(q.base_price),
              "score": round(overlap(text, q.name), 4),
              "category": str(getattr(q.category, "value", q.category))}
             for q in cands),
            key=lambda x: -x["score"],
        )[:top_n]
        top = scored[0]["score"] if scored else 0.0
        second = scored[1]["score"] if len(scored) > 1 else 0.0
        if top >= 0.34 and top >= second * 1.6:
            confidence = "high"
        elif top >= 0.18:
            confidence = "mid"
        else:
            confidence = "low"
        rows.append({
            "item_id": str(it.id), "item_name": it.name, "quantity": str(it.quantity),
            "chapter": chapter, "confidence": confidence, "suggestions": scored,
            "ai_code": None, "reason": f"分类→{chapter or '未识别'}；名称重合度 {top:.2f}",
        })

    # ------------------- AI 兜底：只对不确定的行，且**一次批量**问 ✓
    unclear = [r for r in rows if r["confidence"] != "high" and r["suggestions"]]
    ai_used = False
    if unclear:
        try:
            from app.services.ai_client import chat_json, load_ai_config, require_ready

            cfg = await load_ai_config(db)
            require_ready(cfg)
            lines = []
            for r in unclear:
                cand = "；".join(f"{c['code']} {c['name']}" for c in r["suggestions"])
                lines.append(f"- 行：{r['item_name']}｜候选：{cand}")
            prompt = (
                "下面是报价单里的明细行，以及各自**候选定额子目**。\n"
                "请为每一行从它自己的候选里选一个最合适的，或回答“不确定”。\n"
                "**只能在给定候选里选，不许发明编号** ✗。\n\n" + "\n".join(lines) +
                '\n\n只输出 JSON：{"picks": [{"item": "行名称", "code": "5-x-y 或 空"}]}'
            )
            got = await chat_json(
                db, cfg=cfg,
                system="你是工程造价定额套用助手，只输出 JSON。",
                user_text=prompt, task="quota_preselect",
            )
            picks = {str(p.get("item") or ""): str(p.get("code") or "")
                     for p in (got.get("picks") or [])}
            for r in unclear:
                code = picks.get(r["item_name"], "")
                if code and any(c["code"] == code for c in r["suggestions"]):
                    r["ai_code"] = code      # 只在候选里 ✓
            ai_used = True
        except Exception as exc:  # noqa: BLE001
            for r in unclear:
                r["reason"] += f"；AI 未参与（{type(exc).__name__}）"
    return {"items": rows, "ai_used": ai_used, "pool": len(pool)}
