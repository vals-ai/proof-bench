# Contributing to Proof Bench

NOTE: This was a guide created during the problem collection phase of this benchmark. We are not accepting contributions as of this moment.

This is meant as a guide- sometimes it might be easier to skip some subset of steps but let's generally aim to 
follow this. In general don't waste too much time on the logistical nature of the work- if in some instance you feel this is not the optimal set of steps, can avoid. 

## Step 1: Propose a problem and check before formalizing

Create a GitHub issue with the following information:

- **Source:** Include full details (textbook name, chapter, page number, year, etc.)
- **Informal Statement:** LaTeX formulation of the problem, or image of problem or similar
- **Difficulty & Significance:** Why this problem matters for AI to know, just an informal phrase or a sentence (eg. Dirichlet series are important for analytic number theory and have applications in complex analysis and geometry; therefore they are useful to benchmark models on)
- **Formalizability:** Are all required concepts available in Mathlib? Any anticipated challenges?

Wait for team confirmation before proceeding.

## Step 2: Formalize the Statement

Once your issue is approved, claim it and open a PR with:

1. **Lean file:** `problems/{problem_id}.lean` containing imports and necessary contextormal theorem statement with `sorry`

2. **Informal files:** `problems/informal/{problem_id}_statement.tex` and `problems/informal/{problem_id}_proof.tex`

For the problem ID, use `{topic}_{source}_{identifier}.lean` as the format. Example: `analysis_rudin_ch3_ex15.lean`.


## Step 3: Request Review

Submit your PR with the statement formalization and informal files. The team will review for:
- Correctness of formalization
- Code quality and structure
- Appropriate use of Mathlib
- Clear comments where needed

## Step 4: Complete the Proof

After statement approval, add the formal proof:
- Replace `sorry` with a complete Lean 4 proof
- Ensure all goals are closed
- Add comments to clarify non-obvious steps

## Step 5: CI and Merge

The CI pipeline will:
- Validate metadata headers
- Build the Lean project
- Export to `data/proof-bench.jsonl`

Once CI passes and the PR is approved, it will be merged.

## Notes

- Problems without proofs can be merged if agreed upon by the team
- Keep code clean and well-structured
- Use existing Mathlib lemmas whenever possible
