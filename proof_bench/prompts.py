from typing import Any

SYSTEM_PROMPT = """You are a Lean 4 theorem proving expert and an expert in graduate level mathematics.

You will be given a graduate level mathematics theorem statement and its Lean 4 formalization. You must output
the Lean 4 proof (your output must only have the final proof; just the part starting with `by`).

Pay attention to the way the theorem statement is formalized; there may be a specific nuances in the way the statement has been
formalized. This will be important for you to understand the statement and prove it formally.

You will have access to a set of tools to help you prove the theorem. You may rely on these tools if needed;
be smart about how and when you use them. You may use these tools however you please within the constraints
of the budget and turns, but you must call submit_proof with the final proof (the part starting with `by`) when done.

Use valid, correct Lean 4 syntax; the tools may be helpful if in doubt. Key Lean 4 syntax rules:
- Use CamelCase for lemmas (e.g., Nat.dvd_mul, not nat.dvd_mul)
- Chain tactics with `;` not `,`
- Rewrites require brackets: `rw [h]` or `rw [step1, step2]`
- Use `constructor` for conjunctions, not `split`
- Use `ring` for ring axioms, `linarith` for linear arithmetic
- Existential witnesses: `use 1, 2, 3` not `use [1, 2, 3]`
- No indentation on tactics
- In your final submission, never use `sorry`, even in comments or in the last step.
"""

TOOL_GUIDANCE_TEMPLATE = """BUDGET: {max_turns} turns total. This is the total number of turns you have to submit your final Lean 4 proof for the theorem. Each turn allows UP TO 6 TOOL CALLS MAX (calls beyond 6 are dropped without warning).

⚠️ CRITICAL SCORING RULE: You get 0 points unless you call submit_proof. No submission implies automatic failure.

Tools:
- lean_run_code: Test proofs and iterate on errors
- lean_loogle: Search for existing lemmas and definitions from Mathlib (use this tool carefully and efficiently)
- submit_proof: Submit final proof - REQUIRED TO GET ANY POINTS

IMPORTANT - Tool call strategy:
- You can make up to 6 tool calls per turn. Use them efficiently.
- Example: In one turn, you could call lean_loogle twice AND lean_run_code 4 times (6 total).
- Or call lean_run_code 6 times to test multiple proof variations at once.
- On your final turn, include submit_proof among your tool calls!

Be smart about how you use the tools; here are some tips that you may choose to follow:
- Submit your BEST attempt even if imperfect - an imperfect submission beats no submission
- If you have less than a few turns left, try to STOP exploring and call submit_proof as soon as possible
- If you are confident about your proof early on, call submit_proof immediately
- Never exhaust all {max_turns} turns without calling submit_proof
"""

NO_TOOL_GUIDANCE = ""

USER_PROMPT_TEMPLATE = """Prove the following theorem in Lean 4.

Natural Language Statement:
{natural}
{natural_proof_section}
Formal Statement:
```lean
{header}

{formal}
```

{tool_guidance}

Provide a proof starting with `by`. You must call submit_proof when you want to submit your final proof."""


def build_prompt(
    item: dict[str, Any],
    include_nl_proof: bool = False,
    use_tools: bool = True,
    max_turns: int = 40,
) -> tuple[str, str]:
    """Build system and user prompts from problem data."""
    required = ["natural", "header", "formal"]
    missing = [f for f in required if f not in item]
    if missing:
        raise ValueError(f"Missing required fields: {missing}")

    nl_proof_section = ""
    if include_nl_proof and item.get("nl_proof"):
        nl_proof_section = f"\nNatural Language Proof:\n{item['nl_proof']}\n"

    tool_guidance = TOOL_GUIDANCE_TEMPLATE.format(max_turns=max_turns) if use_tools else NO_TOOL_GUIDANCE

    user_prompt = USER_PROMPT_TEMPLATE.format(
        natural=item["natural"],
        natural_proof_section=nl_proof_section,
        header=item["header"],
        formal=item["formal"],
        tool_guidance=tool_guidance,
    )

    return SYSTEM_PROMPT, user_prompt
