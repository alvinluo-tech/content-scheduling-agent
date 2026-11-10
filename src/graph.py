from __future__ import annotations

import json
from typing import Annotated, Any, TypedDict

from langchain_core.messages import AnyMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from .tools import TOOL_SPECS, Toolbox
from .trace import TraceRecorder


class AgentState(TypedDict, total=False):
    messages: Annotated[list[AnyMessage], add_messages]
    steps: int
    tool_calls: int
    terminal: str


def build_agent_graph(llm: Any, toolbox: Toolbox, trace: TraceRecorder, max_steps: int, system_text: str) -> Any:
    """LangGraph 只承担编排：agent 决策节点 -> tools 执行节点 -> 回到 agent，步数上限在 agent 入口判死。"""
    llm_with_tools = llm.bind_tools(TOOL_SPECS)

    def agent_node(state: AgentState) -> dict[str, Any]:
        steps = state.get("steps", 0)
        if steps >= max_steps:
            trace.close("step_limit_reached")
            return {"terminal": "step_limit_reached"}
        response = llm_with_tools.invoke([SystemMessage(content=system_text), *state["messages"]])
        steps += 1
        tool_calls = getattr(response, "tool_calls", None)
        if not tool_calls:
            content = response.content if isinstance(response.content, str) else json.dumps(response.content, ensure_ascii=False)
            trace.close("completed", content)
            return {"messages": [response], "steps": steps, "terminal": "completed"}
        return {"messages": [response], "steps": steps}

    def tools_node(state: AgentState) -> dict[str, Any]:
        last = state["messages"][-1]
        outputs: list[AnyMessage] = []
        for call in getattr(last, "tool_calls", None) or []:
            name = call["name"]
            args = dict(call.get("args") or {})
            result = toolbox.call(name, args)
            trace.record(state.get("steps", 0), name, args, result, toolbox.summary_of(name, result))
            outputs.append(
                ToolMessage(content=json.dumps(result, ensure_ascii=False), tool_call_id=call["id"], name=name)
            )
        return {"messages": outputs, "tool_calls": state.get("tool_calls", 0) + len(outputs)}

    def route(state: AgentState) -> str:
        if state.get("terminal"):
            return "finish"
        last = state["messages"][-1] if state.get("messages") else None
        return "tools" if getattr(last, "tool_calls", None) else "finish"

    graph: Any = StateGraph(AgentState)
    graph.add_node("agent", agent_node)
    graph.add_node("tools", tools_node)
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", route, {"tools": "tools", "finish": END})
    graph.add_edge("tools", "agent")
    return graph.compile()
