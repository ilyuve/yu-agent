"""SKILL.md 加载器——审查规则归后端组维护，改规则不改代码。"""
from __future__ import annotations

from pathlib import Path


def load_skills(capability_dir: str | Path, skill_names: list[str]) -> str:
    """把 capability 目录下指定 skill 的 SKILL.md 拼成 instructions。"""
    base = Path(capability_dir) / "skills"
    parts = []
    for name in skill_names:
        path = base / name / "SKILL.md"
        if not path.exists():
            raise FileNotFoundError(f"Skill 不存在: {path}")
        parts.append(path.read_text(encoding="utf-8"))
    return "\n\n---\n\n".join(parts)
