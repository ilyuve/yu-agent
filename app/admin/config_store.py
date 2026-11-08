"""控制台配置写回：YAML 读写纯函数（ruamel 往返模式，保留注释与格式）。

设计原则（设计文档 §15）：
- 当前版本不引入配置数据库，界面只是配置文件的编辑器；
- 所有写操作都在这里做校验，非法数据落不了盘；
- 文件操作与 HTTP 层分离，方便单测。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq

_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]*$")

# 能力定义的合法取值（与 app/engine/flows.py 注册表、app/agent/core.py 输出表对齐）
VALID_FLOWS = ["review_pipeline", "agent_only"]
VALID_OUTPUTS = ["review_report", "text"]

_MODEL_FIELDS = ("base_url", "api_key_env", "model", "capabilities",
                 "context_window", "input_price", "output_price")


def _yaml() -> YAML:
    y = YAML()
    y.preserve_quotes = True
    y.width = 4096          # 不主动折行
    y.indent(mapping=2, sequence=4, offset=2)
    return y


def load_yaml(path: Path):
    return _yaml().load(path.read_text(encoding="utf-8"))


def dump_yaml(path: Path, data) -> None:
    import io
    buf = io.StringIO()
    _yaml().dump(data, buf)
    path.write_text(buf.getvalue(), encoding="utf-8")


# ── 模型注册表（models.yaml）──────────────────────────────

def validate_models(models: dict[str, Any], routing: dict[str, Any]) -> None:
    """校验模型注册表与用途路由，不合法抛 ValueError。"""
    if not models:
        raise ValueError("至少保留一个模型")
    for name, m in models.items():
        if not _SLUG.match(name):
            raise ValueError(f"模型名 {name!r} 只能是小写字母/数字/中划线")
        if not isinstance(m, dict):
            raise TypeError(f"模型 {name} 的配置必须是对象")
        if not m.get("model"):
            raise ValueError(f"模型 {name} 缺少 model 字段（模型池里的真实模型名）")
        if not m.get("base_url"):
            raise ValueError(f"模型 {name} 缺少 base_url")
        for price in ("input_price", "output_price"):
            if price in m and (not isinstance(m[price], (int, float)) or m[price] < 0):
                raise ValueError(f"模型 {name} 的 {price} 必须是非负数字")
    for purpose, r in (routing or {}).items():
        primary = (r or {}).get("primary")
        if primary and primary not in models:
            raise ValueError(f"路由 {purpose}.primary 引用了不存在的模型 {primary}")
        for fb in (r or {}).get("fallback", []) or []:
            if fb not in models:
                raise ValueError(f"路由 {purpose}.fallback 引用了不存在的模型 {fb}")


def save_models(models_path: Path, models: dict, routing: dict) -> None:
    """整体写回 models.yaml：在原有 CommentedMap 上就地修改，行内注释保留。

    已存在条目：字段级更新（键上的注释不动）；新模型/新用途追加在末尾；
    界面没提交的已知字段会被移除，用户手写的未知字段保留。
    """
    validate_models(models, routing)
    data = load_yaml(models_path)
    if data is None:
        data = CommentedMap()

    existing = data.get("models")
    if not isinstance(existing, CommentedMap):
        existing = CommentedMap()
        data["models"] = existing
    for k in list(existing.keys()):
        if k not in models:
            del existing[k]
    for name, m in models.items():
        target = existing.get(name)
        if not isinstance(target, CommentedMap):
            target = CommentedMap()
            existing[name] = target
        for field in _MODEL_FIELDS:
            if field in m and m[field] is not None:
                v = m[field]
                if field == "capabilities":
                    seq = CommentedSeq(v or [])
                    seq.fa.set_flow_style()
                    v = seq
                elif field in ("input_price", "output_price"):
                    v = float(v)        # JSON 数字不区分 2 与 2.0，单价统一存浮点
                elif field == "context_window":
                    v = int(v)
                target[field] = v
            elif field in target:
                del target[field]

    old_routing = data.get("routing")
    if not isinstance(old_routing, CommentedMap):
        old_routing = CommentedMap()
        data["routing"] = old_routing
    for k in list(old_routing.keys()):
        if k not in (routing or {}):
            del old_routing[k]
    for purpose, r in (routing or {}).items():
        rm = old_routing.get(purpose)
        if not isinstance(rm, CommentedMap):
            rm = CommentedMap()
            old_routing[purpose] = rm
        rm["primary"] = r.get("primary")
        seq = CommentedSeq(r.get("fallback") or [])
        seq.fa.set_flow_style()
        rm["fallback"] = seq
    dump_yaml(models_path, data)


# ── 能力注册（app/capabilities/<name>/capability.yaml + app.yaml 列表）──

def validate_capability(data: dict[str, Any], partial: bool = False) -> None:
    """校验能力定义。partial=True 表示更新场景，只校验出现的字段。"""
    if (not partial or "name" in data) and not _SLUG.match(str(data.get("name", ""))):
        raise ValueError("能力名只能是小写字母/数字/中划线")
    if "flow" in data and data["flow"] not in VALID_FLOWS:
        raise ValueError(f"flow 必须是 {VALID_FLOWS} 之一")
    if "output" in data and data["output"] not in VALID_OUTPUTS:
        raise ValueError(f"output 必须是 {VALID_OUTPUTS} 之一")


def save_capability(cap_dir: Path, data: dict[str, Any]) -> None:
    """新建/更新能力定义文件。已存在的文件做字段级合并，保留注释。"""
    validate_capability(data, partial=cap_dir.joinpath("capability.yaml").exists())
    cap_dir.mkdir(parents=True, exist_ok=True)
    cap_file = cap_dir / "capability.yaml"
    if cap_file.exists():
        existing = load_yaml(cap_file) or CommentedMap()
    else:
        existing = CommentedMap()
    for key in ("name", "description", "flow", "model_purpose", "output"):
        if key in data and data[key] is not None:
            existing[key] = data[key]
    if data.get("command"):
        trig = existing.get("triggers") or CommentedMap()
        trig["command"] = data["command"]
        existing["triggers"] = trig
    for key in ("skill_names", "toolsets"):
        if key in data and data[key] is not None:
            seq = CommentedSeq(data[key])
            seq.fa.set_flow_style()
            existing[key] = seq
    dump_yaml(cap_file, existing)


def add_capability_path(app_yaml_path: Path, rel_path: str) -> list[str]:
    """把能力目录注册进 app.yaml 的 capabilities 列表（保留注释），返回最新列表。"""
    data = load_yaml(app_yaml_path)
    caps = data.get("capabilities") or CommentedSeq()
    if rel_path in caps:
        raise ValueError(f"能力 {rel_path} 已在注册表中")
    caps.append(rel_path)
    data["capabilities"] = caps
    dump_yaml(app_yaml_path, data)
    return list(caps)


def remove_capability_path(app_yaml_path: Path, rel_path: str) -> list[str]:
    """从 app.yaml 注销能力目录（只改注册表，不删文件），返回最新列表。"""
    data = load_yaml(app_yaml_path)
    caps = data.get("capabilities") or CommentedSeq()
    if rel_path not in caps:
        raise ValueError(f"注册表中不存在 {rel_path}")
    caps.remove(rel_path)
    if not caps:
        raise ValueError("至少保留一个能力")
    data["capabilities"] = caps
    dump_yaml(app_yaml_path, data)
    return list(caps)


# ── 通道与归档（app.yaml 的 adapters/dingtalk/report_sink/dingdoc）──

VALID_ADAPTERS = ["dingtalk"]             # 未来加 feishu 只加这里 + 新 adapter 文件
VALID_SINKS = ["local", "dingdoc"]


def update_channels(app_yaml_path: Path, *, adapters: list[str],
                    dingtalk: dict, report_sink: str, dingdoc: dict) -> None:
    """通道与归档配置写回（就地修改，注释保留）。不合法抛 ValueError。"""
    bad = [a for a in adapters if a not in VALID_ADAPTERS]
    if bad:
        raise ValueError(f"未支持的通道: {bad}（已有: {VALID_ADAPTERS}）")
    if report_sink not in VALID_SINKS:
        raise ValueError(f"report_sink 必须是 {VALID_SINKS} 之一")
    if report_sink == "dingdoc":
        for f in ("workspace_id", "operator_id"):
            if not str(dingdoc.get(f) or "").strip() or str(dingdoc.get(f)).startswith("<"):
                raise ValueError(f"切换 dingdoc 前请先填写 dingdoc.{f}")
    data = load_yaml(app_yaml_path)
    seq = data.get("adapters")
    if not isinstance(seq, CommentedSeq):
        seq = CommentedSeq()
        data["adapters"] = seq
    for item in list(seq):                      # 原地增删，保留存活条目上的注释
        if item not in adapters:
            seq.remove(item)
    for item in adapters:
        if item not in seq:
            seq.append(item)
    dt = data.get("dingtalk") or CommentedMap()
    for f in ("client_id_env", "client_secret_env"):
        if dingtalk.get(f):
            dt[f] = dingtalk[f]
    data["dingtalk"] = dt
    data["report_sink"] = report_sink
    dd = data.get("dingdoc") or CommentedMap()
    for f in ("workspace_id", "operator_id"):
        if dingdoc.get(f) is not None:
            dd[f] = dingdoc[f]
    data["dingdoc"] = dd
    dump_yaml(app_yaml_path, data)
