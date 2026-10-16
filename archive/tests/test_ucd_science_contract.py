"""Compare effective CLI inputs with captured production UCDs commands.

The fixture records the accepted build's actual Click declarations and source
hashes. Real Click parsing tests flag aliases, integer types and implicit
defaults without importing CASA or invoking scientific callbacks.
"""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from askap_auto import science, settings
from template_settings import template_settings

try:
    import click
    from click.testing import CliRunner
except ImportError:
    click = None

BASELINE = json.loads(Path(__file__).with_name("fixtures").joinpath("ucd_science_contract.json").read_text())


class BaselineSettingsTests(unittest.TestCase):
    def test_resources_and_crop_match_production_defaults(self):
        values = template_settings()
        for name, expected in BASELINE["resources"].items():
            with self.subTest(name=name):
                self.assertEqual(values[name], expected)
        self.assertEqual(values["crop_arcmin"], BASELINE["crop_arcmin"])

    def test_template_commands_pass_every_configured_option(self):
        values = template_settings()
        model = science.create_model_command(values, "/work/input.ms", "/work/tmp")
        flags = {token for token in model if token.startswith("--")}
        configured = {"--" + key for key, value in values["model"].items() if value is not None and value is not False}
        self.assertEqual(flags, configured | {"--verbose", "--threads", "--abs-mem", "--temp-dir"})
        self.assertEqual(set(values["model"]), set(settings.MODEL_OPTIONS))
        extract = science.extract_ds_command(values, "Anchor_A", 84.5194, -2.2313,
                                            "/work/input.subtracted.ms", "/work/output.ds")
        self.assertEqual({token for token in extract if token.startswith("--")},
                         {"--datacolumn", "--minuvdist", "--phasecentre", "--baseline-average"})
        self.assertEqual(extract[extract.index("--datacolumn") + 1], "data")

    def test_all_integer_model_fields_refuse_bool_or_fractional_frozen_values(self):
        for key in settings.INTEGER_MODEL_KEYS:
            for value in (True, False, 3.5):
                with self.subTest(key=key, value=value):
                    values = template_settings()
                    values["model"][key] = value
                    with self.assertRaisesRegex(ValueError, "integer for the DStools CLI"):
                        settings.validate(values)
                    with self.assertRaisesRegex(ValueError, "integer for the DStools CLI"):
                        science.model_options(values)

    def test_automatic_child_bind_is_covered_and_explicit_bind_is_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            values = template_settings()
            work = str(Path(temporary).resolve() / "work")
            products = str(Path(work) / "products")
            values.update(work_root=work, product_root=products, container="/accepted.sif",
                          bind=["/explicit/source:/explicit/target"])
            command = science.container_command(values, ["dstools-create-model", "--help"], Path(temporary) / "scratch")
            binds = [command[i + 1] for i, token in enumerate(command) if token == "--bind"]
            self.assertIn(work, binds)
            self.assertNotIn(products, binds)
            self.assertIn("/explicit/source:/explicit/target", binds)

    def test_actual_slot_memory_matches_ucds_for_one_to_four_slots(self):
        values = template_settings()
        for slots in (1, 2, 3, 4):
            with self.subTest(slots=slots):
                budget_mib = 163840 // slots
                expected = max(1, int(budget_mib / 1024 * 0.75))
                self.assertEqual(science.slot_memory(values, slots), (budget_mib, expected))
                command = science.create_model_command(values, "/work/input.ms", "/work/tmp", slots=slots)
                self.assertEqual(command[command.index("--abs-mem") + 1], str(expected))
        values.update(parallel_slots=1, memory_gb=40)
        self.assertEqual(science.slot_memory(values, 1), (40960, 30), "Existing Stage3 allocation stays the same")


@unittest.skipIf(click is None, "Optional host Click unavailable; baseline settings checks still run")
class UCDsCliContractTests(unittest.TestCase):
    def parse(self, tool, arguments):
        spec = BASELINE["commands"][tool]
        params = []
        types = {"int": int, "float": float, "str": str, "Path": Path}
        for entry in spec["options"]:
            options = {key: entry[key] for key in ("default", "is_flag", "nargs") if key in entry}
            if "type" in entry:
                options["type"] = types[entry["type"]]
            if "choices" in entry:
                options["type"] = click.Choice(entry["choices"])
            params.append(click.Option(entry["flags"], **options))
        params += [click.Argument([name], type=Path) for name in spec["arguments"]]
        seen = {}
        def capture(**values):
            seen.update(values)
        command = click.Command(tool, params=params, callback=capture)
        result = CliRunner().invoke(command, arguments)
        self.assertEqual(result.exit_code, 0, result.output)
        return json.loads(json.dumps(seen, default=str))

    def compare(self, arguments):
        tool = arguments[0]
        self.assertEqual(self.parse(tool, arguments[1:]), self.parse(tool, BASELINE["commands"][tool]["ucd_args"]))

    def test_create_model_effective_inputs_equal_ucds(self):
        self.compare(science.create_model_command(template_settings(), "/work/input.ms", "/work/tmp"))

    def test_insert_model_effective_inputs_equal_ucds(self):
        self.compare(science.insert_model_command(template_settings(), 84.5194, -2.2313, "/work/model", "/work/input.ms"))

    def test_subtract_model_effective_inputs_equal_ucds(self):
        self.compare(science.subtract_model_command("/work/input.ms"))

    def test_extract_ds_effective_inputs_equal_ucds(self):
        self.compare(science.extract_ds_command(template_settings(), "Anchor_A", 84.5194, -2.2313,
                                               "/work/input.subtracted.ms", "/work/output.ds"))

    def test_explicit_user_model_overrides_remain_supported_and_typed(self):
        values = template_settings()
        values["model"].update({"imsize": 4096.0, "cell": 2.0, "mgain": 0.7, "threshold": 4.0,
                                "robust": -0.5, "datacolumn": "data", "channels-out": 16.0,
                                "deconvolution-channels": 4.0, "subimages": 2.0, "spectral-pol-terms": 2.0,
                                "minuvw-m": 250.5, "multiscale": False, "multiscale-scale-bias": 0.8,
                                "multiscale-max-scales": 10.0, "mask-threshold": 7.0,
                                "local-rms-window": 10.0, "parallel-gridding": 2.0, "parallel-reordering": 2.0})
        settings.validate(values)
        command = science.create_model_command(values, "/work/input.ms", "/work/tmp")
        parsed = self.parse(command[0], command[1:])
        for key, expected in values["model"].items():
            if expected is not None:
                if key in {"minuvw-m", "minuvw-l"}:
                    expected = str(expected)  # Accepted CLI declares these as TEXT, as in UCDs.
                self.assertEqual(parsed[key.replace("-", "_")], expected)
        self.assertEqual(parsed["savelogs"], False)
        self.assertEqual(parsed["verbose"], True)

    def test_explicit_ds_overrides_do_not_change_other_defaults(self):
        values = template_settings()
        values.update(extract_noflag=True, extract_baseline_average=False, primary_beam="/work/primary.fits",
                      mask_radius_arcsec=20.0)
        command = science.extract_ds_command(values, "Anchor_A", 84.5194, -2.2313,
                                            "/work/input.subtracted.ms", "/work/output.ds")
        parsed = self.parse(command[0], command[1:])
        self.assertTrue(parsed["noflag"])
        self.assertFalse(parsed["baseline_average"])
        self.assertEqual(parsed["datacolumn"], "data")
        self.assertEqual(parsed["primary_beam"], "/work/primary.fits")
        command = science.insert_model_command(values, 84.5194, -2.2313, "/work/model", "/work/input.ms")
        self.assertEqual(self.parse(command[0], command[1:])["mask_radius"], 20.0)


if __name__ == "__main__":
    unittest.main()
