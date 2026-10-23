"""快捷指令模板只在私人产物中注入口令，不执行真实网络请求。"""

import importlib.util
import plistlib
import re
from pathlib import Path

import pytest


@pytest.fixture
def builder():
    path = Path(__file__).resolve().parents[2] / "scripts" / "build_mobile_shortcut.py"
    spec = importlib.util.spec_from_file_location("build_mobile_shortcut", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_shortcut_share_and_clipboard_use_same_authenticated_submission(builder):
    module = builder
    shortcut = module.build_shortcut("http://inbox.test/douyin-inbox/", "test-submit-token-only-123")
    assert plistlib.loads(plistlib.dumps(shortcut)) == shortcut
    actions = shortcut["WFWorkflowActions"]
    identifiers = [item["WFWorkflowActionIdentifier"] for item in actions]
    assert "is.workflow.actions.getclipboard" in identifiers
    assert identifiers.count("is.workflow.actions.downloadurl") == 1
    request = next(item["WFWorkflowActionParameters"] for item in actions
                   if item["WFWorkflowActionIdentifier"].endswith(".downloadurl"))
    assert request["WFHTTPMethod"] == "POST"
    assert request["WFURL"]["Value"]["string"] == "http://inbox.test/douyin-inbox/api/receipts"
    fields = request["WFJSONValues"]["Value"]["WFDictionaryFieldValueItems"]
    assert [item["WFKey"]["Value"]["string"] for item in fields] == ["text", "request_id"]
    assert "ActionExtension" in shortcut["WFWorkflowTypes"]
    with pytest.raises(ValueError):
        module.build_shortcut("http://inbox.test", "")


def test_shortcut_uses_known_ios_actions_and_explicit_input_references(builder):
    # 原生动作名称与参数核对：Shortcuts Toolkit 元数据及已导出的原生快捷指令。
    # https://github.com/viticci/shortcuts-playground-plugin/tree/2de03bffe4ce8802e06d184931d9e4ec366a2ef2/codex/skills/shortcuts-playground/data
    known = {
        "conditional", "getclipboard", "number.random",
        "gettext", "downloadurl", "getvalueforkey", "showresult",
    }
    shortcut = builder.build_shortcut("http://inbox.test", "test-submit-token-only-123")
    seen = set()

    def check_outputs(value):
        if isinstance(value, dict):
            if value.get("Type") == "ActionOutput":
                assert value["OutputUUID"] in seen
            for nested in value.values():
                check_outputs(nested)
        elif isinstance(value, list):
            for nested in value:
                check_outputs(nested)

    for action in shortcut["WFWorkflowActions"]:
        identifier = action["WFWorkflowActionIdentifier"].removeprefix("is.workflow.actions.")
        assert identifier in known
        params = action["WFWorkflowActionParameters"]
        check_outputs(params)
        seen.add(params["UUID"])
        if identifier == "conditional" and params["WFControlFlowMode"] == 0:
            assert params["WFInput"]["Type"] == "Variable"
            assert params["WFInput"]["Variable"]["WFSerializationType"] == "WFTextTokenAttachment"
        if identifier == "getvalueforkey":
            assert params["WFInput"]["WFSerializationType"] == "WFTextTokenAttachment"
            assert "Variable" not in params["WFInput"]
            assert params["WFGetDictionaryValueType"] == "Value"


def test_submission_identifier_uses_real_random_actions_and_valid_token_ranges(builder, tmp_path):
    from skills.douyin_downloader.mobile_inbox import InboxStore

    actions = builder.build_shortcut("http://inbox.test", "test-submit-token-only-123")["WFWorkflowActions"]
    random_actions = [a["WFWorkflowActionParameters"] for a in actions
                      if a["WFWorkflowActionIdentifier"] == "is.workflow.actions.number.random"]
    assert len(random_actions) == 2
    content = next(a["WFWorkflowActionParameters"]["WFTextActionText"]["Value"] for a in actions
                   if a["WFWorkflowActionIdentifier"] == "is.workflow.actions.gettext"
                   and "mobile-" in a["WFWorkflowActionParameters"]["WFTextActionText"]["Value"]["string"])
    simulated = {p["UUID"]: str(p["WFRandomNumberMinimum"]) for p in random_actions}
    result = content["string"]
    for position, reference in sorted(content["attachmentsByRange"].items(), reverse=True):
        start, length = map(int, re.findall(r"\d+", position))
        assert length == 1 and result[start] == "\ufffc"
        result = result[:start] + simulated[reference["OutputUUID"]] + result[start + length:]
    assert re.fullmatch(r"mobile-[0-9]{9}-[0-9]{9}", result)
    store = InboxStore(str(tmp_path / "inbox.db"))
    row = store.submit("https://www.douyin.com/video/123456", result)
    assert row["request_id"] == result


def test_submission_and_feedback_reference_actual_action_outputs(builder):
    actions = builder.build_shortcut("http://inbox.test", "test-submit-token-only-123")["WFWorkflowActions"]
    request = next(a["WFWorkflowActionParameters"] for a in actions
                   if a["WFWorkflowActionIdentifier"].endswith(".downloadurl"))
    outputs = {a["WFWorkflowActionParameters"]["UUID"]: a for a in actions}
    fields = request["WFJSONValues"]["Value"]["WFDictionaryFieldValueItems"]
    for field in fields:
        reference = field["WFValue"]["Value"]["attachmentsByRange"]["{0, 1}"]
        assert reference["Type"] == "ActionOutput"
        source = outputs[reference["OutputUUID"]]
        if field["WFKey"]["Value"]["string"] == "text":
            assert source["WFWorkflowActionIdentifier"].endswith(".conditional")
            assert source["WFWorkflowActionParameters"]["WFControlFlowMode"] == 2
        else:
            assert source["WFWorkflowActionIdentifier"].endswith(".gettext")
    results = [a["WFWorkflowActionParameters"]["Text"] for a in actions
               if a["WFWorkflowActionIdentifier"].endswith(".showresult")]
    assert "先在抖音" in results[1]
    content = results[0]["Value"]
    start, length = map(int, re.findall(r"\d+", next(iter(content["attachmentsByRange"]))))
    assert content["string"][start:start + length] == "\ufffc"
    reference = next(iter(content["attachmentsByRange"].values()))
    assert outputs[reference["OutputUUID"]]["WFWorkflowActionIdentifier"].endswith(".getvalueforkey")
