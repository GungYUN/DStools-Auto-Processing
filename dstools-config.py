"""DStools options and cutout size. CLI > ASKAP_<SETTING> environment > this file.
askap-batch and askap-source read it on every command, from the directory of
config.py (or --dstools-config). There are no built-in defaults: keep every
setting. New values apply to processing that has not completed yet.
"""
# Edit this file directly. Stop the controller and any outstanding Slurm job
# before changing settings for unfinished work, then run the command again.
# ----------------------------------------------------------------------
# 01 dstools-create-model options: DStools long option names without "--".
# Every value is passed to DStools explicitly. None: the option is not passed,
# so DStools computes the value or does not use it. False for a switch: not
# passed. Values marked UCD differ from the DStools defaults and reproduce the
# accepted UCD processing; all others equal the DStools defaults.
# Changing a value changes the model and its identity (no reuse across values).
MODEL = {
    "band": "AK_low",           # UCD (DStools: AT_L). ASKAP: AK_low/AK_mid/AK_high.
    "imsize": None,             # Image size, pixels.
    "cell": None,               # Pixel size, arcsec.
    "iterations": 100000,       # UCD (DStools: 30000).
    "mgain": 0.8,               # Major-cycle gain, (0, 1].
    "threshold": 3,             # Cleaning threshold, sigma; integer.
    "robust": 0.5,              # Briggs robust weighting, [-2, 2].
    "datacolumn": "corrected",  # Column imaged: data or corrected.
    "channels-out": 8,
    "deconvolution-channels": 8,
    "subimages": 1,
    "spectral-pol-terms": 3,
    "multiscale": True,         # UCD (DStools: False).
    "minuvw-m": 200.0,          # UCD (DStools: None). Minimum baseline, metres.
    "minuvw-l": None,           # Minimum baseline, wavelengths; not with minuvw-m.
    "multiscale-scale-bias": 0.7,
    "multiscale-max-scales": 8,
    "fits-mask": None,          # Existing FITS mask, absolute path.
    "target-mask": None,        # Batch mode: one-source catalogues only.
    "galvin-clip-mask": None,
    "erode-beam-shape": False,
    "local-rms-window": None,
    "mask-threshold": 5,        # Integer.
    "phasecentre": None,        # [RA_deg, Dec_deg]; None keeps the MS phase centre.
    "parallel-gridding": None,  # At most THREADS_PER_SLOT.
    "parallel-reordering": None,
}
# 02 dstools-insert-model: target protection radius, arcsec; None: DStools
# uses its beam-based value. Subtraction always writes the residuals to DATA
# of a separate .subtracted.ms.
MASK_RADIUS_ARCSEC = None
# 03 dstools-extract-ds reads the residual DATA column. Baseline cut in metres;
# flags are applied unless EXTRACT_NOFLAG; baselines are averaged unless False.
EXTRACT_MINUV_M = 500.0
EXTRACT_NOFLAG = False
EXTRACT_BASELINE_AVERAGE = True
PRIMARY_BEAM = None  # Existing image path; batch mode: one-source catalogues only.
PRIMARY_BEAM_BY_SOURCE = {}  # {normalised_name: existing_image_path}; no online fetch.
# 04 Cutouts: square side, arcmin; Stokes I and V are both required.
CROP_ARCMIN = 10.0
