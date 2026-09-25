# ADR 0004 — NEWS2 adapted for wearables and ICU replay

**Status:** accepted · 2026-09-25

## Decisions
1. **Partial NEWS2 at home.** A watch measures HR and SpO₂ (spot) but not RR, BP or core temperature. Scores are
   computed from the parameters present and flagged `partial=true` with the missing list. They are never
   treated as normal. Patient-entered BP / temperature from the phone app fill gaps and stay valid for 6 h.
2. **SpO₂ Scale 2** for patients with hypercapnic respiratory failure (COPD cohort), with personal thresholds
   (SpO₂ low 88 %, critical 84 %).
3. **Consciousness scored as *new* change.** The synthetic ICU data sits at GCS 13–14 for most stays; scoring that
   as "C/V" every window would alert constantly. Consciousness is compared to the patient's best GCS this episode
   (drop ≥ 2 or absolute ≤ 12 ⇒ scores 3).
4. **Staleness windows** per parameter and setting (e.g. HR 5 min, spot SpO₂ 30 min at home, 10 min in ICU).
5. **Alarm-fatigue controls.** A red-parameter NEWS2 finding is dropped when a dedicated rule already covers that
   parameter. Per-type suppression lasts 15 min and escalations always pass. Motion-corrupted windows (quality <
   0.6) neither start nor clear a sustained-HR run.

## Consequences
Home alerts rely more on direct rules (SpO₂, sustained HR) than on NEWS2 bands. The evaluation will report
sensitivity separately for partial vs full scores.
