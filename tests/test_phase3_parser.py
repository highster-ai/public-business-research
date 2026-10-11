import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))

import orchestrator
from limits import COUNT_CAP_ERROR, MAX_COUNT


class Phase3ParserTests(unittest.TestCase):
    def test_numeric_boundary_accepted_and_rejected_with_canonical_error(self):
        parsed = orchestrator.parse('Find 60 spas in Dubai Marina, AE.')
        self.assertEqual(parsed['intent']['count'], MAX_COUNT)
        with self.assertRaisesRegex(ValueError, '^' + COUNT_CAP_ERROR + '$'):
            orchestrator.parse('Find 61 spas in Dubai Marina, AE.')

    def test_any_length_integer_gets_cap_error_not_parse_failure(self):
        with self.assertRaisesRegex(ValueError, '^' + COUNT_CAP_ERROR + '$'):
            orchestrator.parse('Find 3000 spas in Dubai Marina, AE.')

    def test_number_word_count_preserves_category(self):
        parsed = orchestrator.parse('Find twenty spas in Seminyak, ID.')
        self.assertEqual(parsed['intent']['count'], 20)
        self.assertEqual(parsed['intent']['category'], 'spas')
        self.assertEqual(parsed['intent']['area'], 'Seminyak')
        self.assertEqual(parsed['intent']['country'], 'ID')

    def test_compound_number_words_through_limit(self):
        cases = [('twenty one', 21), ('thirty-five', 35),
                 ('fifty nine', 59), ('sixty', 60)]
        for words, expected in cases:
            with self.subTest(words=words):
                parsed = orchestrator.parse(
                    'Find %s wellness spas in Dubai Marina, AE.' % words)
                self.assertEqual(parsed['intent']['count'], expected)
                self.assertEqual(parsed['intent']['category'], 'wellness spas')

    def test_area_is_not_augmented(self):
        parsed = orchestrator.parse('Find 5 spas in Seminyak, ID.')
        self.assertEqual(parsed['intent']['area'], 'Seminyak')

    def test_country_can_be_explicit_clause(self):
        parsed = orchestrator.parse('Find 5 spas in Dubai Marina, country AE.')
        self.assertEqual(parsed['intent']['area'], 'Dubai Marina')
        self.assertEqual(parsed['intent']['country'], 'AE')
        self.assertNotIn('country', parsed['flags']['missing'])

    def test_missing_country_is_reported_and_noninteractive_spec_stops(self):
        parsed = orchestrator.parse('Find 5 spas in Seminyak.')
        self.assertIsNone(parsed['intent']['country'])
        self.assertIn('country', parsed['flags']['missing'])
        result = orchestrator.spec_command('Find 5 spas in Seminyak.')
        self.assertEqual(result['status'], 'missing')
        self.assertIn('country', result['flags']['missing'])

    def test_country_required_for_structured_noninteractive_intent(self):
        intent = orchestrator.parse('Find 5 spas in Seminyak, ID.')['intent']
        intent['country'] = None
        with self.assertRaisesRegex(ValueError, 'country is required'):
            orchestrator.validate_intent(intent)

    def test_dm_measured_not_default_but_remains_explicit(self):
        default = orchestrator.parse('Find 5 spas in Seminyak, ID.')['intent']
        self.assertNotIn('dm_measured', default['requested_fields'])
        explicit = orchestrator.parse(
            'Find 5 spas in Seminyak, ID. I need dm measured.')['intent']
        self.assertIn('dm_measured', explicit['requested_fields'])
        self.assertIn('business_name', explicit['requested_fields'])

    def test_orchestrator_validation_uses_same_cap_error(self):
        intent = orchestrator.parse('Find 60 spas in Dubai Marina, AE.')['intent']
        orchestrator.validate_intent(intent)
        with self.assertRaisesRegex(ValueError, '^' + COUNT_CAP_ERROR + '$'):
            orchestrator.validate_intent(dict(intent, count=61))


if __name__ == '__main__':
    unittest.main()
