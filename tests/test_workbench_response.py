import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from openchatbi.llm.llm import call_llm_chat_model_with_retry
from sample_ui.response import resolve_visible_response


def test_non_streaming_final_answer_is_visible():
    result = resolve_visible_response("", {"messages": [HumanMessage(content="question"), AIMessage(content="answer")]})
    assert result.text == "answer" and not result.is_error


def test_final_message_overrides_intermediate_token_text():
    assert (
        resolve_visible_response("I will check. Answer", {"messages": [AIMessage(content="Answer")]}).text == "Answer"
    )


def test_failed_turn_cannot_redisplay_the_previous_answer():
    result = resolve_visible_response(
        "",
        {
            "messages": [AIMessage(content="old answer"), HumanMessage(content="new question")],
            "final_answer": "old answer",
        },
    )
    assert result.is_error and "old answer" not in result.text


def test_tool_output_is_not_mistaken_for_an_answer():
    result = resolve_visible_response("", {"messages": [ToolMessage(content="internal output", tool_call_id="call")]})
    assert result.is_error and "internal output" not in result.text


@pytest.mark.parametrize("status", [401, 403])
def test_authentication_error_is_visible_without_retry_or_secret(status):
    error = RuntimeError("provider error with sensitive credentials")
    error.status_code = status
    llm = Mock()
    llm.invoke.side_effect = error
    response = call_llm_chat_model_with_retry(llm, [HumanMessage(content="question")])
    assert llm.invoke.call_count == 1
    assert "sensitive credentials" not in response.content
    assert str(status) in response.content
    visible = resolve_visible_response("", {"messages": [response]})
    assert visible.is_error and visible.text == response.content


def test_generic_model_failure_has_a_visible_error():
    result = resolve_visible_response(
        "", {"messages": [AIMessage(content="Sorry, the LLM service is currently unavailable.")]}
    )
    assert result.is_error and "模型服务调用失败" in result.text


def test_stream_only_provider_and_empty_state():
    assert resolve_visible_response("streamed answer", {}).text == "streamed answer"
    assert resolve_visible_response("", {}).is_error


@pytest.mark.asyncio
@pytest.mark.parametrize("failed", [False, True])
async def test_real_page_flow_renders_tokenless_result_and_correct_status(failed):
    from openchatbi.streaming import AgentStreamProcessor

    file = Path(__file__).resolve().parents[1] / "sample_ui/streamlit_ui.py"
    tree = ast.parse(file.read_text(encoding="utf-8"))
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "process_user_message_stream"
    )
    response = AIMessage(
        content="HTTP 401: model access failed" if failed else "The answer is 30.",
        response_metadata={"llm_error": failed},
    )

    async def no_tokens(*args, **kwargs):
        for item in ():
            yield item

    graph = SimpleNamespace(
        astream=no_tokens,
        aget_state=AsyncMock(
            return_value=SimpleNamespace(
                interrupts=(), values={"messages": [HumanMessage(content="question"), response]}
            )
        ),
    )
    manager = SimpleNamespace(_initialized=True, get_graph=AsyncMock(return_value=graph))
    rendered = Mock()
    namespace = {
        "st": SimpleNamespace(session_state=SimpleNamespace(graph_manager=manager, session_interrupts={})),
        "AgentStreamProcessor": AgentStreamProcessor,
        "resolve_visible_response": resolve_visible_response,
        "process_download_links": lambda value: value,
        "render_content_with_downloads": rendered,
    }
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(file), "exec"), namespace)
    thinking = Mock()
    result = await namespace[function.name]("question", "user", "session", None, thinking, MagicMock())
    rendered.assert_called_once_with(response.content)
    assert result[0] == result[-1] == response.content
    if failed:
        assert "Analysis complete" not in result[3]
        assert "请求失败" in result[3]
    else:
        assert "Analysis complete" in result[3]
