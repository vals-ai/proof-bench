/-
Source: Probability Theory: STAT310/MATH230 by Amir Dembo, April 15, 2021, Exercise 1.1.5
Statement: Prove that a finitely additive non-negative set function `μ` on a measurable space
  `(Ω, ℱ)` with the "continuity" property
  `Bₙ ∈ ℱ, Bₙ ↓ ∅, μ(Bₙ) < ∞ → μ(Bₙ) → 0`
  must be countably additive if `μ(Ω) < ∞`.
-/

import Mathlib

open TopologicalSpace Filter MeasureTheory ProbabilityTheory Function

open scoped NNReal ENNReal MeasureTheory ProbabilityTheory Topology

namespace MeasureTheory

variable {Ω : Type*} [MeasurableSpace Ω]

/-- A finitely additive non-negative set function `μ` on a measurable space
`(Ω, ℱ)` with the "continuity" property
`Bₙ ∈ ℱ, Bₙ ↓ ∅, μ(Bₙ) < ∞ → μ(Bₙ) → 0`
must be countably additive if `μ(Ω) < ∞`.

Remark. Note that `μ(Ω) < ∞` automatically in the following statement since `μ` takes value
in `ℝ≥0`. -/
theorem dembo_1_1_15 {μ : Set Ω → ℝ≥0}
    (hAdd : ∀ A B, MeasurableSet A → MeasurableSet B → Disjoint A B → μ (A ∪ B) = μ A + μ B)
    (hCont : ∀ B : ℕ → Set Ω,
      (∀ n, MeasurableSet (B n)) → Antitone B → (⋂ n, B n) = ∅
      → Tendsto (fun n => μ (B n)) atTop (𝓝 0))
    {A : ℕ → Set Ω} (hA : ∀ n, MeasurableSet (A n)) (hDisj : Pairwise (Disjoint on A)) :
    μ (⋃ n, A n) = ∑' n, μ (A n) := by
  sorry

end MeasureTheory