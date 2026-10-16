"""Complete settings read from the repository's config.py and dstools-config.py templates.

The program has no built-in defaults, so tests start from the shipped templates.
"""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from askap_auto import settings


def template_settings():
    values, _ = settings.load(ROOT / "config.py", ROOT / "dstools-config.py", validate_values=False)
    return values


def write_settings(config, values):
    """Write complete settings as config.py plus dstools-config.py in the same directory."""
    config = Path(config)
    for path, keys in ((config, settings.CONFIG_KEYS), (settings.dstools_config_path(config), settings.DSTOOLS_KEYS)):
        path.write_text("".join(f"{key.upper()} = {values[key]!r}\n" for key in keys))
    return config
