# Proof Bench Setup

This document contains the engineering and environment setup details for working on Proof Bench locally.

## Python

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
uv venv
source .venv/bin/activate
uv pip install -e ".[llm]"
```

For development without LLM integrations:

```bash
uv pip install -e .
```

`[llm]` includes the public `model-library` dependency for model access.

## Lean 4

```bash
curl https://raw.githubusercontent.com/leanprover/elan/master/elan-init.sh -sSf | sh
source "$HOME/.elan/env"
lake update
lake exe cache get
lake build
```

The benchmark uses Lean `v4.25.2` and Mathlib `v4.25.2`.

## Export And Validate Problems

Any change to `problems/` should be followed by:

```bash
python proof_bench/validate_and_export.py
```

This validates required metadata headers and rebuilds `data/proof-bench.jsonl`.

## Running The Benchmark

Basic run:

```bash
python main.py --dataset exported --model openai/gpt-4o --k 3
```

Include natural-language proof hints:

```bash
python main.py --dataset exported --model openai/gpt-4o --k 3 --include-nl-proof
```

Restrict to one problem:

```bash
python main.py --dataset exported --problem-id algebraicGeometry_vakil_ch_11_2_E --model openai/gpt-4o
```

Restrict by domain substring:

```bash
python main.py --dataset exported --domains logic number_theory --model openai/gpt-4o
```

## MCP And Loogle Setup

Proof Bench uses [lean-lsp-mcp](https://github.com/oOo0oOo/lean-lsp-mcp) to provide the Lean tools.

Install it with:

```bash
uv tool install lean-lsp-mcp
```

Run with Loogle enabled:

```bash
python main.py --dataset exported --model openai/gpt-4o --k 3 \
  --enable-loogle --loogle-local
```

Modes:
- `--loogle-local`: local search index, no web rate limits
- no `--loogle-local`: remote Loogle behavior
- `LOOGLE_DAEMON_URL=http://127.0.0.1:8765`: shared daemon mode

You can also set `LEAN_LOOGLE_LOCAL=true` instead of passing `--loogle-local`.

## Building The Local Loogle Cache

The local cache must match the exact Lean/Mathlib versions used by this repo.

```bash
rm -rf ~/.cache/lean-lsp-mcp/loogle

mkdir -p ~/.cache/lean-lsp-mcp
cd ~/.cache/lean-lsp-mcp
git clone --depth 1 https://github.com/nomeata/loogle.git loogle/repo
cd loogle/repo

echo "leanprover/lean4:v4.25.2" > lean-toolchain
sed -i 's|@ "master"|@ "v4.25.2"|' lakefile.lean

# Lean 4.25.2 compatibility fixes
sed -i 's/(s\\.drop i)\\.copy/s.drop i/' Loogle/Find.lean
sed -i 's/\\.trimAscii\\.copy/.trim/' Loogle.lean

lake update
lake exe cache get
lake build

mkdir -p ../index
MATHLIB_REV=$(python3 -c "import json; m=json.load(open('lake-manifest.json')); print([p['rev'][:12] for p in m['packages'] if p['name']=='mathlib'][0])")
.lake/build/bin/loogle --json --write-index "../index/mathlib-${MATHLIB_REV}.idx" ""
```

What that does:
- clears any stale local loogle cache
- clones and pins a local Loogle checkout to this repo's Lean/Mathlib version
- applies small Lean 4.25.2 compatibility patches
- builds the Loogle binary
- generates a version-matched local search index

Verify:

```bash
echo 'Nat.add_comm' | ~/.cache/lean-lsp-mcp/loogle/repo/.lake/build/bin/loogle --json --interactive
```

## Shared Loogle Daemon

For parallel runs, shared-daemon mode is the most memory-efficient option.

```bash
# Terminal 1
python -m proof_bench.loogle_daemon --port 8765

# Terminal 2+
export LOOGLE_DAEMON_URL=http://127.0.0.1:8765
python main.py --dataset exported --model openai/gpt-4o --k 8 --enable-loogle
```

Programmatic config:

```python
loogle_config = {
    "loogle_daemon_url": "http://127.0.0.1:8765",
    "max_results": 8,
}
```

## Development

```bash
pre-commit install
pre-commit run --all-files
ruff check .
ruff format .
lake build
pytest tests/
```

## Upgrading Lean Or Mathlib

1. Update `lean-toolchain`.
2. Update the Mathlib version in `lakefile.lean`.
3. Run:

```bash
lake update
lake exe cache get
lake build
python proof_bench/validate_and_export.py
```

4. Rebuild the local Loogle cache if you use local search.
