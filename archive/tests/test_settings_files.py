"""config.py and dstools-config.py are the only sources of setting values."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from askap_auto import cli, settings
from template_settings import ROOT, template_settings, write_settings

BASELINE = json.loads(Path(__file__).with_name("fixtures").joinpath("ucd_science_contract.json").read_text())
UCD_OVERRIDES = {"band": "AK_low", "iterations": 100000, "multiscale": True, "minuvw-m": 200.0}


class SettingsFileTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.values = template_settings()
        self.config = write_settings(self.root / "config.py", self.values)
        self.dstools = settings.dstools_config_path(self.config)

    def remove_lines(self, path, *prefixes):
        lines = path.read_text().splitlines(keepends=True)
        path.write_text("".join(line for line in lines if not line.startswith(prefixes)))

    def test_templates_define_every_setting_exactly_once(self):
        config = settings.read_settings_file(ROOT / "config.py", "config.py")
        dstools = settings.read_settings_file(ROOT / "dstools-config.py", "dstools-config.py")
        self.assertEqual(set(config), set(settings.CONFIG_KEYS))
        self.assertEqual(set(dstools), set(settings.DSTOOLS_KEYS))
        self.assertFalse(set(settings.CONFIG_KEYS) & set(settings.DSTOOLS_KEYS))
        self.assertEqual(list(dstools["model"]), list(settings.MODEL_OPTIONS))
        settings.validate(template_settings())

    def test_template_model_is_dstools_defaults_plus_four_ucd_values(self):
        model = template_settings()["model"]
        declared = {entry["flags"][-1].split("/")[0][2:]: entry.get("default")
                    for entry in BASELINE["commands"]["dstools-create-model"]["options"]}
        for option in settings.MODEL_OPTIONS:
            with self.subTest(option=option):
                expected = UCD_OVERRIDES.get(option, declared[option])
                self.assertEqual(model[option], expected)
        ucd = BASELINE["commands"]["dstools-create-model"]["ucd_args"]
        self.assertEqual(ucd[ucd.index("-B") + 1], model["band"])
        self.assertEqual(int(ucd[ucd.index("-N") + 1]), model["iterations"])

    def test_missing_settings_name_their_file(self):
        self.remove_lines(self.config, "POLL_SECONDS")
        self.remove_lines(self.dstools, "CROP_ARCMIN")
        values = settings.read_settings_file(self.dstools, "dstools-config.py")
        del values["model"]["robust"]
        self.dstools.write_text("".join(f"{key.upper()} = {value!r}\n" for key, value in values.items()))
        with self.assertRaises(ValueError) as caught:
            settings.load(self.config, self.dstools)
        message = str(caught.exception)
        self.assertIn("no built-in defaults", message)
        self.assertIn("config.py: POLL_SECONDS", message)
        self.assertIn("dstools-config.py: CROP_ARCMIN, MODEL['robust']", message)

    def test_setting_in_the_wrong_file_is_refused(self):
        with self.config.open("a") as handle:
            handle.write("CROP_ARCMIN = 5.0\n")
        with self.assertRaisesRegex(ValueError, r"CROP_ARCMIN belong\(s\) in dstools-config.py"):
            settings.load(self.config, self.dstools)
        with self.config.open("a") as handle:
            handle.write("EXTRACT_DATACOLUMN = 'data'\n")
        with self.assertRaisesRegex(ValueError, "Unknown setting.*EXTRACT_DATACOLUMN"):
            settings.load(self.config, self.dstools)

    def test_dstools_config_is_read_next_to_config_unless_given(self):
        other = self.root / "other" / "my-dstools.py"
        other.parent.mkdir()
        other.write_text(self.dstools.read_text().replace("CROP_ARCMIN = 10.0", "CROP_ARCMIN = 4.0"))
        common = ["run", "--config", str(self.config), "--work-dir", str(self.root / "work")]
        with patch.dict(os.environ, {}, clear=True):
            values, origins = cli.settings_from_args(cli.build_parser(False).parse_args(common), False)
            self.assertEqual((values["crop_arcmin"], origins["crop_arcmin"]), (10.0, str(self.dstools.resolve())))
            values, origins = cli.settings_from_args(
                cli.build_parser(True).parse_args(common + ["--dstools-config", str(other)]), True)
            self.assertEqual((values["crop_arcmin"], origins["crop_arcmin"]), (4.0, str(other.resolve())))
        self.dstools.unlink()
        with patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(ValueError, "Settings file not found: .*dstools-config.py"):
            cli.settings_from_args(cli.build_parser(False).parse_args(common), False)

    def test_command_line_and_environment_still_override_the_files(self):
        argv = ["run", "--config", str(self.config), "--work-dir", str(self.root / "work"),
                "--model-option", "robust=-0.5", "--crop-arcmin", "6"]
        with patch.dict(os.environ, {"ASKAP_EXTRACT_MINUV_M": "250"}, clear=True):
            values, origins = cli.settings_from_args(cli.build_parser(False).parse_args(argv), False)
        self.assertEqual((values["model"]["robust"], origins["model.robust"]), (-0.5, "CLI"))
        self.assertEqual(values["model"]["iterations"], 100000)
        self.assertEqual((values["crop_arcmin"], values["extract_minuv_m"]), (6.0, 250))


if __name__ == "__main__":
    unittest.main()
