/-
Source: Vakil, The Rising Sea : Foundations of Algebraic Geometry (Princeton University Press, 2025), p. 316, Example 11.2.E.
Statement:
Show that an $A$-scheme is separated (over $A$) if and only if it is separated over $\mathbb{Z}$.
-/

import Mathlib

open AlgebraicGeometry

variable {A : Type*} {X : Scheme} [CommRing A]
    (struct_X : X ⟶ Spec (CommRingCat.of A))

theorem algebraicGeometry_vakil_ch_11_2_E :
    IsSeparated struct_X ↔ IsSeparated (instOverTerminalScheme X).hom := sorry