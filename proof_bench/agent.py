"""Agent that orchestrates LLM interactions with Lean tools for theorem proving.

Uses model_library Agent for the conversation loop.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from model_library.agent import (
    Agent,
    AgentConfig,
    AgentHooks,
    AgentResult,
    AgentTurn,
    ErrorTurn,
    SerializableException,
    TurnLimit,
    TurnResult,
)
from model_library.base import TextInput
from model_library.base.input import InputItem, RawResponse, SystemInput, ToolDefinition
from model_library.registry_utils import get_registry_model

from .tools import LoogleTool, RunCodeTool, SubmitProofTool, ToolConfig

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Hook functions
# ---------------------------------------------------------------------------


def _turn_status(turn_number: int, max_turns: int) -> str:
    """Human-readable turn status string."""
    remaining = max(0, max_turns - turn_number)
    if remaining == 0:
        return "[⚠️ FINAL TURN - Call submit_proof NOW or get 0 points!]"
    if remaining == 1:
        return f"[⚠️ Turn {turn_number}/{max_turns} - NEXT TURN IS LAST! Call submit_proof NOW!]"
    if remaining <= 3:
        return f"[⚠️ Turn {turn_number}/{max_turns} - ONLY {remaining} TURNS LEFT! Call submit_proof immediately!]"
    if remaining <= 5:
        return f"[Turn {turn_number}/{max_turns} - {remaining} left. Call submit_proof soon or you will fail!]"
    if remaining <= 10:
        return f"[Turn {turn_number}/{max_turns} - {remaining} left. Start preparing your submission.]"
    return f"[Turn {turn_number}/{max_turns}, {remaining} remaining]"


def _turn_message(turn_number: int, max_turns: int) -> InputItem | None:
    return TextInput(text=_turn_status(turn_number, max_turns))


def _tool_filter(turn_number: int, max_turns: int, tools: list[ToolDefinition]) -> list[ToolDefinition]:
    if turn_number >= max_turns:
        return [t for t in tools if t.name == "submit_proof"]
    return tools


def _should_stop(turn_result: TurnResult) -> bool:
    # Default would stop on text-only responses (no tool calls). Override to keep
    # the agent running — _before_query injects a continuation prompt instead.
    return False


def _before_query(history: list[InputItem], last_error: Exception | None) -> list[InputItem]:
    if last_error:
        raise last_error
    # RawResponse is the model's raw response and is always appended to history.
    # If it's still the last item (no ToolResults following it), the model responded
    # with text only — inject a continuation prompt to keep the agent going.
    if history and isinstance(history[-1], RawResponse):
        history.append(
            TextInput(
                text=(
                    "Continue your solution attempt. You can use lean_loogle and lean_run_code "
                    "tools as needed. When your solution is complete, call submit_proof - your "
                    "work is only graded if you call this tool, and you cannot continue after "
                    "calling it."
                )
            )
        )
    return history


def _determine_answer(
    state: dict[str, Any],
    turns: list[AgentTurn | ErrorTurn],
    final_error: SerializableException | None,
) -> str:
    return state.get("proof_text", "")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def run_agent(
    model_str: str,
    query_text: str,
    system_prompt: str | None = None,
    loogle_config: ToolConfig | None = None,
    run_code_config: ToolConfig | None = None,
    problem_context: dict[str, str] | None = None,
    max_turns: int = 40,
    question_id: str = "proof",
    log_dir: Path = Path("logs"),
) -> AgentResult:
    """Run the proof agent and return AgentResult."""
    model = get_registry_model(model_str)

    tools = []
    if loogle_config and model.supports_tools:
        tools.append(LoogleTool(loogle_config))
    if run_code_config and model.supports_tools:
        tools.append(RunCodeTool(run_code_config))
    if model.supports_tools:
        tools.append(SubmitProofTool(run_code_config, problem_context or {}))

    config = AgentConfig(
        turn_limit=TurnLimit(
            max_turns=max_turns,
            turn_message=_turn_message,
            tool_filter=_tool_filter,
        ),
        time_limit=None,
        max_tool_calls_per_turn=6,
    )

    hooks = AgentHooks(
        should_stop=_should_stop,
        before_query=_before_query,
        determine_answer=_determine_answer,
    )

    input_items: list[InputItem] = []
    if system_prompt:
        input_items.append(SystemInput(text=system_prompt))
    input_items.append(TextInput(text=query_text))

    agent = Agent(
        llm=model,
        tools=tools,
        name="proof-bench",
        log_dir=log_dir,
        config=config,
        hooks=hooks,
    )

    return await agent.run(
        input=input_items,
        question_id=question_id,
    )
