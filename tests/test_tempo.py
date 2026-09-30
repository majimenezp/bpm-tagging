import unittest

from bpm_tagging.analysis import choose_tempo_detailed, tempo_candidates


class TempoSelectionTests(unittest.TestCase):
    def test_candidates_include_normal_half_and_double_when_in_range(self) -> None:
        self.assertEqual(tempo_candidates(120), [60, 120, 240])

    def test_equal_evidence_is_explicitly_ambiguous_without_policy(self) -> None:
        decision = choose_tempo_detailed(90, {90: 0.8, 180: 0.8})
        self.assertTrue(decision.ambiguous)
        self.assertIn(decision.alternate_bpm, {90, 180})
        self.assertEqual({decision.bpm, decision.alternate_bpm}, {90, 180})

    def test_preferred_range_is_an_explicit_policy(self) -> None:
        decision = choose_tempo_detailed(90, {90: 0.8, 180: 0.8}, preferred_range=(160, 200))
        self.assertEqual(decision.bpm, 180)
        self.assertFalse(decision.ambiguous)

    def test_detector_strength_wins_when_evidence_is_clear(self) -> None:
        decision = choose_tempo_detailed(128, {64: 0.3, 128: 0.9})
        self.assertEqual((decision.bpm, decision.alternate_bpm), (128, 64))
        self.assertGreater(decision.candidate_margin, 0.5)

    def test_bpm_outside_configured_range_is_not_a_candidate(self) -> None:
        self.assertEqual(tempo_candidates(200, minimum=160, maximum=220), [200])
