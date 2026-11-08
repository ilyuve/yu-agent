"""慢查静态预扫（设计文档 §6 阶段①）——确定性规则，配置驱动，不改代码。"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_SQL_SUFFIX = {".sql", ".xml"}          # MyBatis mapper / migration
_CODE_SUFFIX = {".java", ".py", ".go", ".kt", ".sql", ".xml"}


@dataclass
class ScanHit:
    rule_id: str
    severity: str
    file: str
    line: int
    snippet: str
    description: str


def load_rules(cfg: dict) -> list[dict]:
    return cfg.get("rules", [])


def scan(workdir: str, rules: list[dict], changed_files: list[str] | None = None) -> list[ScanHit]:
    """对工作目录（可选限定变更文件）执行规则预扫，输出候选点清单。"""
    compiled = [(r, re.compile(r["pattern"])) for r in rules if r.get("pattern_type") == "regex"]
    hits: list[ScanHit] = []
    root = Path(workdir)
    files = [root / f for f in changed_files] if changed_files else (
        p for p in root.rglob("*") if p.suffix in _CODE_SUFFIX and p.is_file())
    for path in files:
        if not path.exists() or path.suffix not in _CODE_SUFFIX:
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for lineno, text in enumerate(lines, 1):
            for rule, pattern in compiled:
                if pattern.search(text):
                    hits.append(ScanHit(rule["id"], rule["severity"],
                                        str(path.relative_to(root)), lineno,
                                        text.strip()[:200], rule["description"]))
    return hits


def format_hits(hits: list[ScanHit]) -> str:
    if not hits:
        return "静态预扫未发现候选点。"
    lines = [f"静态预扫候选点（{len(hits)} 个）："]
    for h in hits:
        lines.append(f"- [{h.severity}] {h.rule_id} {h.file}:{h.line} — {h.description}\n  `{h.snippet}`")
    return "\n".join(lines)
