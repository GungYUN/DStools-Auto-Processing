"""Exercise the confirmed DStools INT threshold contract with real Click parsing.

The two option declarations reproduce the accepted build source. No scientific
callback, MS data, container, or network is required. Click is optional on the
host; the settings/serialization checks still run without it.
"""
import copy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from askap_auto import science, settings
from template_settings import template_settings

try:
    import click
    from click.testing import CliRunner
except ImportError:
    click = None


def threshold_tokens(values):
    tokens = science.model_options(values)
    return [token for key in settings.INTEGER_THRESHOLD_KEYS
            if "--" + key in tokens
            for token in tokens[tokens.index("--" + key):tokens.index("--" + key) + 2]]


class ModelThresholdTests(unittest.TestCase):
    def test_template_thresholds_are_passed_as_integers_without_mutation(self):
        values = json.loads(json.dumps(template_settings()))
        values["model"].update(threshold=3.0, **{"mask-threshold": 5.0})
        before = copy.deepcopy(values)
        self.assertEqual(threshold_tokens(values), ["--threshold", "3", "--mask-threshold", "5"])
        self.assertEqual(values, before)
        tokens = science.model_options(values)
        self.assertEqual(tokens[tokens.index("--mgain") + 1], "0.8")
        self.assertEqual(tokens[tokens.index("--robust") + 1], "0.5")

    def test_nondefault_integral_float_thresholds_emit_integers_without_mutation(self):
        values = template_settings()
        values["model"].update(threshold=4.0, **{"mask-threshold": 7.0})
        before = copy.deepcopy(values)
        self.assertEqual(threshold_tokens(values), ["--threshold", "4", "--mask-threshold", "7"])
        self.assertEqual(values, before)

    def test_custom_integral_and_zero_thresholds_are_preserved(self):
        values = template_settings()
        values["model"].update(threshold=0.0, **{"mask-threshold": 7.0})
        settings.validate(values)
        self.assertEqual(threshold_tokens(values), ["--threshold", "0", "--mask-threshold", "7"])

    def test_invalid_thresholds_are_rejected_before_freezing(self):
        for key in settings.INTEGER_THRESHOLD_KEYS:
            for value in (3.5, -1, True, False, "3", float("nan"), float("inf")):
                with self.subTest(key=key, value=value):
                    values = template_settings()
                    values["model"][key] = value
                    with self.assertRaisesRegex(ValueError, "nonnegative integer for the DStools CLI"):
                        settings.validate(values)

    def test_invalid_frozen_thresholds_are_not_rounded_or_silently_skipped(self):
        for key in settings.INTEGER_THRESHOLD_KEYS:
            for value in (3.5, True, False):
                with self.subTest(key=key, value=value):
                    values = template_settings()
                    values["model"][key] = value
                    with self.assertRaisesRegex(ValueError, "nonnegative integer for the DStools CLI"):
                        science.model_options(values)

    def test_none_threshold_uses_dstools_default_by_omission(self):
        values = template_settings()
        values["model"].update(threshold=None, **{"mask-threshold": None})
        settings.validate(values)
        tokens = science.model_options(values)
        self.assertNotIn("--threshold", tokens)
        self.assertNotIn("--mask-threshold", tokens)


@unittest.skipIf(click is None, "Optional host Click is unavailable; serializer/settings checks still run")
class DStoolsClickThresholdContractTests(unittest.TestCase):
    def setUp(self):
        @click.command()
        @click.option("-t", "--threshold", default=3)
        @click.option("-m", "--mask-threshold", default=5)
        def command(threshold, mask_threshold):
            click.echo(json.dumps([threshold, mask_threshold]))
        self.command = command
        self.runner = CliRunner()

    def test_old_decimal_tokens_reproduce_both_exit2_failures(self):
        for key, value in (("threshold", "3.0"), ("mask-threshold", "5.0")):
            with self.subTest(key=key):
                result = self.runner.invoke(self.command, ["--" + key, value])
                self.assertEqual(result.exit_code, 2)
                self.assertIn("not a valid integer", result.output)

    def test_fixed_generated_tokens_pass_actual_click_parser(self):
        result = self.runner.invoke(self.command, threshold_tokens(template_settings()))
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(json.loads(result.output), [3, 5])

    def test_legacy_omission_and_new_explicit_options_have_identical_numeric_values(self):
        legacy = self.runner.invoke(self.command, [])
        generated = self.runner.invoke(self.command, threshold_tokens(template_settings()))
        self.assertEqual(legacy.exit_code, 0, legacy.output)
        self.assertEqual(generated.exit_code, 0, generated.output)
        self.assertEqual(json.loads(legacy.output), json.loads(generated.output))


if __name__ == "__main__":
    unittest.main()
