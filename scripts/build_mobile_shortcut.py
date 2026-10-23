"""生成私人快捷指令；口令仅从环境读取，不写入项目目录。"""

import argparse
import os
import plistlib
import uuid
from pathlib import Path
from urllib.parse import urlsplit


def text(value: str | dict) -> dict:
    content = {"string": value, "attachmentsByRange": {}} if isinstance(value, str) else {
        "string": "\ufffc", "attachmentsByRange": {"{0, 1}": value},
    }
    return {"WFSerializationType": "WFTextTokenString", "Value": content}


def dictionary(fields: dict) -> dict:
    return {"WFSerializationType": "WFDictionaryFieldValue", "Value": {
        "WFDictionaryFieldValueItems": [
            {"WFItemType": 0, "WFKey": text(key), "WFValue": value}
            for key, value in fields.items()
        ],
    }}


def build_shortcut(server: str, token: str) -> dict:
    """分享菜单接收文字/链接；直接启动时改用剪贴板。"""
    parsed = urlsplit(server)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.query or parsed.fragment:
        raise ValueError("服务器地址格式不正确")
    if len(token) < 20:
        raise ValueError("收件口令未配置")
    extension = {"Type": "ExtensionInput", "VariableName": "ShortcutInput"}
    group = str(uuid.uuid4()).upper()
    actions = []

    def attachment(value: dict) -> dict:
        return {"WFSerializationType": "WFTextTokenAttachment", "Value": value}

    def condition_input(value: dict) -> dict:
        # 条件判断与普通动作的输入类型不同，不能共用同一层包装。
        return {"Type": "Variable", "Variable": attachment(value)}

    def action(identifier: str, **parameters) -> dict:
        output_id = str(uuid.uuid4()).upper()
        parameters["UUID"] = output_id
        actions.append({"WFWorkflowActionIdentifier": "is.workflow.actions." + identifier,
                        "WFWorkflowActionParameters": parameters})
        return {"Type": "ActionOutput", "OutputUUID": output_id, "OutputName": "结果"}

    action("conditional", GroupingIdentifier=group, WFControlFlowMode=0, WFCondition=100,
           WFInput=condition_input(extension))
    action("gettext", WFTextActionText=text(extension))
    action("conditional", GroupingIdentifier=group, WFControlFlowMode=1)
    action("getclipboard")
    shared = action("conditional", GroupingIdentifier=group, WFControlFlowMode=2)
    input_group = str(uuid.uuid4()).upper()
    action("conditional", GroupingIdentifier=input_group, WFControlFlowMode=0, WFCondition=100,
           WFInput=condition_input(shared))
    # Shortcuts 没有 generateuuid 操作；使用原生随机数生成每次提交编号。
    first = action("number.random", WFRandomNumberMinimum=100000000, WFRandomNumberMaximum=999999999)
    second = action("number.random", WFRandomNumberMinimum=100000000, WFRandomNumberMaximum=999999999)
    request_id = action("gettext", WFTextActionText={"WFSerializationType": "WFTextTokenString", "Value": {
        "string": "mobile-\ufffc-\ufffc", "attachmentsByRange": {"{7, 1}": first, "{9, 1}": second},
    }})
    response = action("downloadurl", WFURL=text(server.rstrip("/") + "/api/receipts"), WFHTTPMethod="POST",
                      WFHTTPBodyType="JSON", WFHTTPHeaders=dictionary({"Authorization": text("Bearer " + token)}),
                      WFJSONValues=dictionary({"text": text(shared), "request_id": text(request_id)}))
    message = action("getvalueforkey", WFDictionaryKey="message", WFGetDictionaryValueType="Value",
                     WFInput=attachment(response))
    action("showresult", Text={"WFSerializationType": "WFTextTokenString", "Value": {
        "string": "抖音看板：\n\ufffc", "attachmentsByRange": {"{6, 1}": message},
    }})
    action("conditional", GroupingIdentifier=input_group, WFControlFlowMode=1)
    action("showresult", Text="没有收到视频链接。请先在抖音点击分享、复制链接，再运行此快捷指令。")
    action("conditional", GroupingIdentifier=input_group, WFControlFlowMode=2)
    return {
        "WFWorkflowActions": actions,
        "WFWorkflowClientVersion": "3030.0.4.2",
        "WFWorkflowMinimumClientVersion": 900,
        "WFWorkflowMinimumClientVersionString": "900",
        "WFWorkflowIcon": {"WFWorkflowIconStartColor": 4282601983, "WFWorkflowIconGlyphNumber": 59511},
        "WFWorkflowInputContentItemClasses": ["WFStringContentItem", "WFURLContentItem"],
        "WFWorkflowOutputContentItemClasses": [],
        "WFWorkflowImportQuestions": [],
        "WFWorkflowTypes": ["ActionExtension"],
        "WFWorkflowHasShortcutInputVariables": True,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="生成发送到抖音看板的私人快捷指令")
    parser.add_argument("--server", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    token = os.environ.get("DOUYIN_INBOX_SUBMIT_TOKEN", "")
    os.umask(0o077)
    args.output.write_bytes(plistlib.dumps(build_shortcut(args.server, token)))


if __name__ == "__main__":
    main()
