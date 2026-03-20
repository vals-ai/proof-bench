# Proof Bench

Automated theorem proving benchmark for Lean 4. The overall objective is to create 
a benchmark that tests AI models' ability to solve problems at the advanced undergrad or graduate level, in Lean.

Proof Bench evaluates models on Lean 4 mathematics problems using a small tool-enabled proving loop:
- `lean_run_code` for compilation feedback
- `lean_loogle` for lemma and definition search
- `submit_proof` for final graded submission

The benchmark currently runs on the exported dataset via `--dataset exported`.

Setup, local Loogle configuration, development commands, and platform run instructions live in `SETUP.md`.

## Repo Structure

- `problems/`: Lean theorem files plus informal statements/proofs under `problems/informal/`. The folder only has sample problems; the rest of the benchmark is private.
- `data/proof-bench.jsonl`: exported metadata used at runtime
- `proof_bench/agent.py`: agent loop and tool orchestration
- `proof_bench/tools.py`: `lean_run_code`, `lean_loogle`, and `submit_proof` tool subclasses
- `proof_bench/mcp_client.py`: MCP client infrastructure and Lean execution
- `proof_bench/prover.py`: attempt execution, aggregation, and logging
- `proof_bench/load_problems.py`: exported dataset loader
- `proof_bench/validate_and_export.py`: metadata validation and JSONL export
- `main.py`: CLI entrypoint

## Quick Start

After following `SETUP.md`, export the dataset then run:

```bash
python proof_bench/validate_and_export.py
python main.py --dataset exported --model openai/gpt-4o --k 3
```

Useful variants:

```bash
python main.py --dataset exported --model openai/gpt-4o --k 3 --include-nl-proof
python main.py --dataset exported --problem-id algebraicGeometry_vakil_ch_11_2_E --model openai/gpt-4o
python main.py --dataset exported --domains logic number_theory --model openai/gpt-4o
```

## Where To Look Next

- `SETUP.md`: installation, MCP/Loogle setup, local cache generation, development workflow, CI, and platform runs
- `proof_bench/tools.py`: tool subclasses (`lean_run_code`, `lean_loogle`, `submit_proof`)
- `proof_bench/mcp_client.py`: MCP client infrastructure and Lean execution
- `proof_bench/agent.py`: agent loop
- `proof_bench/prover.py`: attempt execution and result aggregation
