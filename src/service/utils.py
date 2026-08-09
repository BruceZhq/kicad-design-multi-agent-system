from typing import Any, cast

from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    RemoveMessage,
    ToolMessage,
)
from langchain_core.messages import (
    ChatMessage as LangchainChatMessage,
)

from schema import ChatMessage
from service.llm_output import provider_reasoning_content, response_text


def explicit_reasoning_content(message: AIMessage | AIMessageChunk) -> str:
    """Return provider-visible reasoning without inventing hidden chain-of-thought."""
    return provider_reasoning_content(message)


def convert_message_content_to_string(content: str | list[str | dict]) -> str:
    return response_text(content)


def langchain_to_chat_message(message: BaseMessage) -> ChatMessage:
    """Create a ChatMessage from a LangChain message."""
    match message:
        case HumanMessage():
            human_message = ChatMessage(
                type="human",
                content=convert_message_content_to_string(message.content),
            )
            return human_message
        case AIMessage():
            ai_message = ChatMessage(
                type="ai",
                content=convert_message_content_to_string(message.content),
            )
            if message.tool_calls:
                ai_message.tool_calls = message.tool_calls
            if message.response_metadata:
                ai_message.response_metadata = message.response_metadata
            reasoning = explicit_reasoning_content(message)
            if reasoning:
                ai_message.custom_data["reasoning_content"] = reasoning
            return ai_message
        case ToolMessage():
            tool_message = ChatMessage(
                type="tool",
                content=convert_message_content_to_string(message.content),
                tool_call_id=message.tool_call_id,
            )
            return tool_message
        case LangchainChatMessage():
            if message.role == "custom":
                custom_message = ChatMessage(
                    type="custom",
                    content="",
                    custom_data=cast(dict[str, Any], message.content[0]),
                )
                return custom_message
            else:
                raise ValueError(f"Unsupported chat message role: {message.role}")
        case _:
            raise ValueError(f"Unsupported message type: {message.__class__.__name__}")


def try_stream_chat_message(message: Any) -> tuple[ChatMessage | None, str | None]:
    """Adapt one display message without turning presentation drift into run failure."""
    try:
        return langchain_to_chat_message(message), None
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


def execution_error_payload(error: Exception) -> dict[str, Any]:
    """Build the fatal stream envelope reserved for agent execution failures."""
    detail = str(error).strip() or type(error).__name__
    return {
        "type": "error",
        "content": f"{type(error).__name__}: {detail}",
        "code": "agent_execution_error",
        "retryable": False,
    }


def visible_stream_messages(messages: list[Any]) -> list[Any]:
    """Exclude LangGraph state mutations from the user-facing message stream."""
    return [message for message in messages if not isinstance(message, RemoveMessage)]


def remove_tool_calls(content: str | list[str | dict]) -> str | list[str | dict]:
    """Remove tool calls from content."""
    if isinstance(content, str):
        return content
    # Currently only Anthropic models stream tool calls, using content item type tool_use.
    return [
        content_item
        for content_item in content
        if isinstance(content_item, str) or content_item["type"] != "tool_use"
    ]
