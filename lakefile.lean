import Lake
open Lake DSL

package «proof-bench» where
  leanOptions := #[
    ⟨`autoImplicit, false⟩,
    ⟨`relaxedAutoImplicit, false⟩
  ]

require mathlib from git
  "https://github.com/leanprover-community/mathlib4.git" @ "v4.25.2"

@[default_target]
lean_lib problems where
  globs := #[.submodules `problems]
