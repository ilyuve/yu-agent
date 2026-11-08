"""慢查静态预扫的单测——也是 Evals「CI 数据判」的起点（设计文档 §12）。

跑法：uv run pytest tests/test_scanner.py
"""
from __future__ import annotations

import shutil
from pathlib import Path

import yaml

from app.tools.local.scanner import scan

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = PROJECT_ROOT / "tests" / "fixtures"


def _load_rules() -> list[dict]:
    cfg = yaml.safe_load((PROJECT_ROOT / "config" / "scan_rules.yaml").read_text(encoding="utf-8"))
    return cfg["rules"]


def test_scanner_hits_known_slow_queries(tmp_path: Path):
    """埋了问题的 mapper 必须命中 SQ001 / SQ002 / SQ003。"""
    shutil.copy(FIXTURES / "sample_mapper.xml", tmp_path / "sample_mapper.xml")

    hits = scan(str(tmp_path), _load_rules())
    hit_ids = {h.rule_id for h in hits}

    assert "SQ001" in hit_ids, "SELECT * 未命中"
    assert "SQ002" in hit_ids, "无 WHERE 的 DELETE 未命中"
    assert "SQ003" in hit_ids, "LIKE 前缀通配未命中"
    # 命中要带上定位信息，方便报告引用
    for h in hits:
        assert h.file.endswith("sample_mapper.xml")
        assert h.line > 0


def test_scanner_passes_clean_code(tmp_path: Path):
    """干净代码零误报——守护规则集的误报率。"""
    shutil.copy(FIXTURES / "clean_dao.py", tmp_path / "clean_dao.py")

    hits = scan(str(tmp_path), _load_rules())

    assert hits == [], f"干净代码被误报: {[h.rule_id for h in hits]}"


def test_scanner_scoped_to_changed_files(tmp_path: Path):
    """指定变更文件时，只扫这些文件（MR 增量审查路径）。"""
    shutil.copy(FIXTURES / "sample_mapper.xml", tmp_path / "sample_mapper.xml")
    shutil.copy(FIXTURES / "clean_dao.py", tmp_path / "clean_dao.py")

    hits = scan(str(tmp_path), _load_rules(), changed_files=["clean_dao.py"])

    assert hits == []
