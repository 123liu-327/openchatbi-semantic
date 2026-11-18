"""Resolve visible turn output even when the model emits no token events."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import AIMessage

from openchatbi.utils import get_text_from_content


@dataclass(frozen=True)
class VisibleResponse:
    text: str
    is_error: bool = False


def resolve_visible_response(streamed: str, values: Mapping[str, Any]) -> VisibleResponse:
    messages = values.get("messages") or []
    if messages and isinstance(messages[-1], AIMessage) and not messages[-1].tool_calls:
        message = messages[-1]
        text = get_text_from_content(message.content).strip()
        if text:
            if text == "Sorry, the LLM service is currently unavailable.":
                return VisibleResponse(
                    "模型服务调用失败，未生成回答。请检查 .env 中的密钥和接口地址，修改后重启工作台。", True
                )
            return VisibleResponse(
                text,
                bool(message.response_metadata.get("llm_error") or message.response_metadata.get("workflow_error")),
            )
    # A final_answer field may be left over from the preceding turn. Never use
    # it when the current message history ends with a user/tool message.
    if not messages and values.get("final_answer"):
        return VisibleResponse(str(values["final_answer"]))
    if streamed.strip():
        return VisibleResponse(streamed.strip())
    return VisibleResponse("本次请求没有生成回答，请检查模型服务配置或工作台日志。", True)
