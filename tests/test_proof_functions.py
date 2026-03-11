from proof_bench.utils import (
    _detect_commented_lines,
    _strip_leading_empty_lines,
    _strip_response_and_format_proof,
    strip_comment_blocks,
)


def test_strip_leading_empty_lines():
    """Test removal of leading empty lines."""
    lines = ["", "", "import Mathlib", "", "def hello := 42"]
    expected = ["import Mathlib", "", "def hello := 42"]
    assert _strip_leading_empty_lines(lines) == expected


def test_single_line_comments():
    """Test detection of single-line -- comments."""
    lines = ["-- single line comment", "import Mathlib", "def hello := 42"]
    expected = [True, False, False]
    assert _detect_commented_lines(lines) == expected


def test_multi_line_comments():
    """Test detection of multi-line /- ... -/ comments."""
    lines = ["/- multi", "line", "-/", "theorem test : true"]
    expected = [True, True, True, False]
    assert _detect_commented_lines(lines) == expected


def test_single_line_inside_multi_line():
    """Test that single-line comments inside multi-line comments are still commented."""
    lines = ["/- multi-line", "-- this is still commented", "-/", "normal code"]
    expected = [True, True, True, False]
    assert _detect_commented_lines(lines) == expected


def test_single_line_doc_comment():
    """Test that /-- ... -/ doc-comments on one line don't leak into subsequent lines."""
    lines = ["/-- Doc comment -/", "theorem foo := by", "  sorry"]
    expected = [True, False, False]
    assert _detect_commented_lines(lines) == expected


def test_normal_code_is_uncommented():
    """Test that normal lean code is uncommented.
    This is taken from measure_dembo_1-1-5.lean
    This also tests doc-comments (/--)"""

    lines = [
        "/-",
        "Source: Probability Theory: STAT310/MATH230 by Amir Dembo, April 15, 2021, Exercise 1.1.5",
        "Statement: Prove that a finitely additive non-negative set function `μ` on a measurable space",
        '  `(Ω, ℱ)` with the "continuity" property',
        "  `Bₙ ∈ ℱ, Bₙ ↓ ∅, μ(Bₙ) < ∞ → μ(Bₙ) → 0`",
        "  must be countably additive if `μ(Ω) < ∞`.",
        "-/",
        "",
        "import Mathlib",
        "",
        "open TopologicalSpace Filter MeasureTheory ProbabilityTheory Function",
        "",
        "open scoped NNReal ENNReal MeasureTheory ProbabilityTheory Topology",
        "",
        "namespace MeasureTheory",
        "",
        "variable {Ω : Type*} [MeasurableSpace Ω]",
        "",
        "/-- A finitely additive non-negative set function `μ` on a measurable space",
        '`(Ω, ℱ)` with the "continuity" property',
        "`Bₙ ∈ ℱ, Bₙ ↓ ∅, μ(Bₙ) < ∞ → μ(Bₙ) → 0`",
        "must be countably additive if `μ(Ω) < ∞`.",
        "",
        "Remark. Note that `μ(Ω) < ∞` automatically in the following statement since `μ` takes value",
        "in `ℝ≥0`. -/",
        "theorem dembo_1_1_15 {μ : Set Ω → ℝ≥0}",
        "    (hAdd : ∀ A B, MeasurableSet A → MeasurableSet B → Disjoint A B → μ (A ∪ B) = μ A + μ B)",
        "    (hCont : ∀ B : ℕ → Set Ω,",
        "      (∀ n, MeasurableSet (B n)) → Antitone B → (⋂ n, B n) = ∅",
        "      → Tendsto (fun n => μ (B n)) atTop (𝓝 0))",
        "    {A : ℕ → Set Ω} (hA : ∀ n, MeasurableSet (A n)) (hDisj : Pairwise (Disjoint on A)) :",
        "    μ (⋃ n, A n) = ∑' n, μ (A n) := by",
        "  sorry",
        "",
        "end MeasureTheory",
    ]

    expected = [
        True,
        True,
        True,
        True,
        True,
        True,
        True,  # multi-line comment
        False,
        False,
        False,
        False,
        False,
        False,
        False,
        False,
        False,
        False,
        False,
        True,
        True,
        True,
        True,
        True,
        True,
        True,  # another multi-line comment
        False,
        False,
        False,
        False,
        False,
        False,
        False,  # theorem definition
        False,
        False,
        False,
    ]

    assert _detect_commented_lines(lines) == expected

    # Test that strip_comment_blocks produces the correct cleaned code
    code = "\n".join(lines)
    expected_stripped = (
        "\n"  # Empty line after removing initial comments
        "import Mathlib\n"
        "\n"
        "open TopologicalSpace Filter MeasureTheory ProbabilityTheory Function\n"
        "\n"
        "open scoped NNReal ENNReal MeasureTheory ProbabilityTheory Topology\n"
        "\n"
        "namespace MeasureTheory\n"
        "\n"
        "variable {Ω : Type*} [MeasurableSpace Ω]\n"
        "\n"
        "theorem dembo_1_1_15 {μ : Set Ω → ℝ≥0}\n"
        "    (hAdd : ∀ A B, MeasurableSet A → MeasurableSet B → Disjoint A B → μ (A ∪ B) = μ A + μ B)\n"
        "    (hCont : ∀ B : ℕ → Set Ω,\n"
        "      (∀ n, MeasurableSet (B n)) → Antitone B → (⋂ n, B n) = ∅\n"
        "      → Tendsto (fun n => μ (B n)) atTop (𝓝 0))\n"
        "    {A : ℕ → Set Ω} (hA : ∀ n, MeasurableSet (A n)) (hDisj : Pairwise (Disjoint on A)) :\n"
        "    μ (⋃ n, A n) = ∑' n, μ (A n) := by\n"
        "  sorry\n"
        "\n"
        "end MeasureTheory"
    )

    assert strip_comment_blocks(code) == expected_stripped


def test_strip_response_and_format_proof_lean_fence():
    """Test extraction from ```lean ... ```."""
    input_text = """Here's some explanation
```lean
def hello := "world"
```
More text"""
    expected = 'by\n  def hello := "world"'
    assert _strip_response_and_format_proof(input_text) == expected


def test_strip_response_and_format_proof_generic_fence():
    """Test extraction from ```...```."""
    input_text = """Some text
```
theorem test : true := trivial
```
End"""
    expected = "by\n  theorem test : true := trivial"
    assert _strip_response_and_format_proof(input_text) == expected


def test_strip_response_and_format_proof_cleanup():
    """Test removal of leading :="""
    input_text = ":= by simp"
    expected = "by simp"
    assert _strip_response_and_format_proof(input_text) == expected


def test_strip_response_and_format_proof_already_formatted():
    """Test that already properly formatted proofs are left unchanged."""
    input_text = "by\n  simp\n  done"
    expected = "by\n  simp\n  done"
    assert _strip_response_and_format_proof(input_text) == expected


def test_strip_response_and_format_proof_fenced_with_by():
    """Test fenced block extraction with existing 'by' keyword."""
    input_text = """Here is the proof:
```lean
by
  simp
  trivial
```
That's it!"""
    expected = "by\n  simp\n  trivial"
    assert _strip_response_and_format_proof(input_text) == expected


def test_axiom_in_proof():
    """Test whether the stripping function is able to successfully detect `axiom`s in the proof"""

    input_proof_with_axiom = """
by
  -- Declare a “local axiom” inside the proof
  have absurd_axiom : False := by
    axiom

  exact False.elim absurd_axiom
"""

    # we'd expect it to return None
    expected = None

    assert _strip_response_and_format_proof(input_proof_with_axiom) == expected


def test_local_instance_in_proof():
    """Test detection of `local_instance` in the proof."""
    input_proof_with_local_instance = """
by
  local_instance : Inhabited Nat := ⟨0⟩
  have h : Nat := default
  exact h
"""
    expected = None

    assert _strip_response_and_format_proof(input_proof_with_local_instance) == expected
