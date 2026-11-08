"""控制台配置写回（app/admin/config_store.py）测试。"""
import pytest
import yaml

from app.admin.config_store import (
    add_capability_path,
    load_yaml,
    remove_capability_path,
    save_capability,
    save_models,
    update_channels,
    validate_models,
)

MODELS_YAML = """# 模型注册表（公司模型池，OpenAI 兼容）+ 用途路由 + 单价
models:
  qwen-plus:
    base_url: "http://llm-pool.example.com/v1"    # 占位符
    api_key_env: LLM_POOL_KEY
    model: "qwen-plus"
    capabilities: [tool_call]
    context_window: 131072
    input_price: 0.8              # 元/百万 input tokens
    output_price: 2.0

routing:                        # 按用途路由，Agent 只认 purpose 不认模型名
  review:
    primary: qwen-plus
    fallback: []
"""

APP_YAML = """# 服务级配置
server:
  workers: 3

capabilities:
  - app/capabilities/code-review    # flow: review_pipeline
  - app/capabilities/qa             # flow: agent_only
  # 未来新增能力只加一行
"""


@pytest.fixture
def models_file(tmp_path):
    p = tmp_path / "models.yaml"
    p.write_text(MODELS_YAML, encoding="utf-8")
    return p


@pytest.fixture
def app_file(tmp_path):
    p = tmp_path / "app.yaml"
    p.write_text(APP_YAML, encoding="utf-8")
    return p


# ── 模型注册表 ──

def test_save_models_add_and_rewrite(models_file):
    data = load_yaml(models_file)
    models = dict(data["models"])
    models["deepseek-v3"] = {
        "base_url": "http://llm-pool.example.com/v1", "api_key_env": "LLM_POOL_KEY",
        "model": "deepseek-v3", "capabilities": ["tool_call"],
        "context_window": 65536, "input_price": 0.5, "output_price": 1.5,
    }
    routing = {"review": {"primary": "qwen-plus", "fallback": ["deepseek-v3"]},
               "chat": {"primary": "deepseek-v3", "fallback": ["qwen-plus"]}}
    save_models(models_file, models, routing)

    text = models_file.read_text(encoding="utf-8")
    assert "模型注册表" in text          # 顶部注释保留
    assert "# 占位符" in text            # 已有条目的行内注释保留
    assert "# 按用途路由" in text or "按用途路由" in text
    reloaded = yaml.safe_load(text)
    assert set(reloaded["models"]) == {"qwen-plus", "deepseek-v3"}
    assert reloaded["models"]["deepseek-v3"]["output_price"] == 1.5
    assert reloaded["routing"]["review"]["fallback"] == ["deepseek-v3"]
    # 无关字段不混入
    assert "hack" not in reloaded["models"]["qwen-plus"]


def test_validate_models_rejects_bad_routing():
    with pytest.raises(ValueError, match="不存在的模型"):
        validate_models({"a": {"model": "m", "base_url": "u"}},
                        {"review": {"primary": "ghost", "fallback": []}})
    with pytest.raises(ValueError, match="至少保留一个模型"):
        validate_models({}, {})
    with pytest.raises(ValueError, match="base_url"):
        validate_models({"a": {"model": "m"}}, {})


# ── 能力注册 ──

def test_save_capability_create_and_update(tmp_path):
    cap_dir = tmp_path / "app" / "capabilities" / "sql-review"
    save_capability(cap_dir, {
        "name": "sql-review", "description": "慢查专项审查",
        "flow": "review_pipeline", "model_purpose": "review",
        "output": "review_report", "command": "sqlreview",
        "skill_names": ["slow-query"], "toolsets": ["local.git"],
    })
    f = cap_dir / "capability.yaml"
    cap = yaml.safe_load(f.read_text(encoding="utf-8"))
    assert cap["name"] == "sql-review"
    assert cap["triggers"]["command"] == "sqlreview"
    assert cap["skill_names"] == ["slow-query"]

    # 更新：只改描述，其余字段不动
    save_capability(cap_dir, {"name": "sql-review", "description": "新描述"})
    cap2 = yaml.safe_load(f.read_text(encoding="utf-8"))
    assert cap2["description"] == "新描述"
    assert cap2["flow"] == "review_pipeline"      # 未提交的字段保留
    assert cap2["toolsets"] == ["local.git"]


def test_save_capability_rejects_bad_flow(tmp_path):
    with pytest.raises(ValueError, match="flow"):
        save_capability(tmp_path / "x", {"name": "x", "flow": "magic"})


def test_capability_path_add_remove(app_file):
    caps = add_capability_path(app_file, "app/capabilities/sql-review")
    assert caps[-1] == "app/capabilities/sql-review"
    text = app_file.read_text(encoding="utf-8")
    assert "未来新增能力只加一行" in text          # 注释保留

    with pytest.raises(ValueError, match="已在注册表"):
        add_capability_path(app_file, "app/capabilities/sql-review")

    caps = remove_capability_path(app_file, "app/capabilities/sql-review")
    assert "app/capabilities/sql-review" not in caps

    with pytest.raises(ValueError, match="不存在"):
        remove_capability_path(app_file, "app/capabilities/ghost")


def test_remove_capability_keeps_at_least_one(tmp_path):
    p = tmp_path / "app.yaml"
    p.write_text("capabilities:\n  - app/capabilities/qa\n", encoding="utf-8")
    with pytest.raises(ValueError, match="至少保留一个能力"):
        remove_capability_path(p, "app/capabilities/qa")


# ── 通道与归档 ──

CHANNELS_YAML = """# 服务级配置
adapters:
  - dingtalk                    # 未来加 feishu 只加一行
dingtalk:
  client_id_env: DINGTALK_CLIENT_ID
  client_secret_env: DINGTALK_CLIENT_SECRET
report_sink: local              # local = 本地文件
dingdoc:
  workspace_id: "<fill-in>"
  operator_id: "<fill-in>"
"""


def test_update_channels_roundtrip(tmp_path):
    p = tmp_path / "app.yaml"
    p.write_text(CHANNELS_YAML, encoding="utf-8")
    update_channels(p, adapters=["dingtalk"],
                    dingtalk={"client_id_env": "DT_ID", "client_secret_env": "DT_SECRET"},
                    report_sink="dingdoc",
                    dingdoc={"workspace_id": "ws123", "operator_id": "op9"})
    text = p.read_text(encoding="utf-8")
    assert "未来加 feishu" in text                    # 注释保留
    cfg = yaml.safe_load(text)
    assert cfg["report_sink"] == "dingdoc"
    assert cfg["dingdoc"]["workspace_id"] == "ws123"
    assert cfg["dingtalk"]["client_id_env"] == "DT_ID"


def test_update_channels_rejects_dingdoc_without_fields(tmp_path):
    p = tmp_path / "app.yaml"
    p.write_text(CHANNELS_YAML, encoding="utf-8")
    with pytest.raises(ValueError, match="workspace_id"):
        update_channels(p, adapters=["dingtalk"], dingtalk={},
                        report_sink="dingdoc", dingdoc={})
    with pytest.raises(ValueError, match="未支持的通道"):
        update_channels(p, adapters=["wechat"], dingtalk={},
                        report_sink="local", dingdoc={})
