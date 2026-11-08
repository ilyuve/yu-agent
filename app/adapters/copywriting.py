"""消息文案风格库（受理/完成成对，按任务随机抽一套，同一任务内人格一致）。

- 每个任务受理时由装配层随机抽一个风格下标存进 task.params["_style"]（随任务落库），
  完成通知按同一下标配对渲染，避免"受理是骑士、完成变零零后"的人格分裂；
- params 里没有 "_style"（如重启恢复的存量任务）时默认用 0 号稳重风；
- 加新风格 = 在 STYLES 末尾追加一对模板，{id} 为任务号占位。
"""
from __future__ import annotations

# (受理模板, 完成模板)；{id} = 任务号
STYLES: list[tuple[str, str]] = [
    # A 稳重专业
    ("✅ 收到，任务 #{id} 已开始审查，完事儿叫你",
     "✅ 审查完成 #{id}"),
    # B 活泼工友
    ("🫡 收到！#{id} 已开工，我去啃代码了，看完喊你",
     "📋 啃完了 #{id}，报告如下"),
    # F 打工人嘴替
    ("🫡 收到收到！#{id} 这就安排，代码我先替你看看有没有埋雷",
     "🧹 雷排完了 #{id}，战报如下"),
    # G 游戏开黑
    ("🎮 #{id} 已进副本，正在刷怪中，通关了喊你",
     "🏆 通关了 #{id}，掉落如下"),
    # H 外卖小哥
    ("🏍️ 您的大单 #{id} 已接单！骑士飞速审查中，马上送达",
     "📦 您的大单 #{id} 已送达，请查收"),
    # I 摸鱼搭子
    ("👀 行，#{id} 我瞅瞅去。你喝口水，一会儿给你汇报",
     "🗣️ 瞅完了 #{id}，跟你汇报下"),
    # J 热血中二
    ("🔥 #{id} 已锁定目标！慢查询和烂代码一个都别想跑",
     "⚡ 收工 #{id}，逮到的都在这儿了"),
    # L 干脆利落零零后
    ("ok，#{id} 在看了在看了 🏃",
     "看完了 #{id} 🫡"),
]

DEFAULT_STYLE = 0


def _index(params: dict) -> int:
    i = params.get("_style", DEFAULT_STYLE)
    return i if isinstance(i, int) and 0 <= i < len(STYLES) else DEFAULT_STYLE


def accepted(params: dict, task_id: str) -> str:
    return STYLES[_index(params)][0].format(id=task_id)


def done(params: dict, task_id: str) -> str:
    return STYLES[_index(params)][1].format(id=task_id)
