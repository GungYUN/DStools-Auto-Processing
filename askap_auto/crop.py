"""Crop WSClean FITS images while preserving useful celestial WCS.

English: The image is cropped around the proper-motion-corrected ICRS
position, written through a temporary file, and atomically published to the explicit output.

中文：图像围绕经过自行修正的 ICRS 位置裁剪，先写入临时文件，再原子写入指定输出路径。
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import astropy.units as u
import numpy as np
from astropy.coordinates import SkyCoord
from astropy.io import fits
from astropy.nddata import Cutout2D
from astropy.wcs import WCS


_COPY_HEADER_KEYS = (
    "BUNIT",
    "BMAJ",
    "BMIN",
    "BPA",
    "DATE-OBS",
    "MJD-OBS",
    "OBJECT",
    "TELESCOP",
    "RESTFRQ",
    "SPECSYS",
)


def _as_2d(data: np.ndarray) -> np.ndarray:
    """Reduce a FITS array to its first 2-D image plane.

    中文：把 FITS 数组取到第一个 2-D 图像平面。
    """

    data = np.asarray(data)
    while data.ndim > 2:
        data = data[0]
    if data.ndim != 2:
        raise ValueError(f"Expected a 2-D image after squeezing, got {data.shape}")
    return data


def crop_fits(path: Path, ra_deg: float, dec_deg: float, size_arcmin: float, output: Path | None = None) -> None:
    """Crop around the corrected position; output=None retains legacy in-place behaviour.

    English: Preserve useful observation/header metadata while replacing the
    celestial WCS with the cutout WCS.

    中文：保留有用的观测/header 元数据，并用 cutout 的天球 WCS 替换原 WCS。
    """

    path = Path(path)
    with fits.open(path, memmap=False) as hdul:
        original_header = hdul[0].header.copy()
        data = _as_2d(hdul[0].data)
        celestial_wcs = WCS(original_header).celestial

    position = SkyCoord(ra=ra_deg * u.deg, dec=dec_deg * u.deg, frame="icrs")
    cutout = Cutout2D(
        data,
        position=position,
        size=(size_arcmin * u.arcmin, size_arcmin * u.arcmin),
        wcs=celestial_wcs,
        mode="partial",
        fill_value=np.nan,
        copy=True,
    )

    header = cutout.wcs.to_header()
    for key in _COPY_HEADER_KEYS:
        if key in original_header:
            header[key] = original_header[key]
    header["HISTORY"] = f"Cropped to {size_arcmin:g} arcmin by Ozstar ASKAP pipeline"

    output = Path(output) if output is not None else path
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.parent / f".{output.name}.crop.tmp"
    fits.PrimaryHDU(data=np.asarray(cutout.data), header=header).writeto(
        temporary, overwrite=True
    )
    os.replace(temporary, output)


def main() -> None:
    """Crop one FITS image from command-line coordinates and size.

    中文：根据命令行坐标和尺寸裁剪一个 FITS 图像。
    """

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("fits", type=Path)
    parser.add_argument("ra", type=float)
    parser.add_argument("dec", type=float)
    parser.add_argument("--size-arcmin", type=float, default=10.0)
    args = parser.parse_args()
    crop_fits(args.fits, args.ra, args.dec, args.size_arcmin)


if __name__ == "__main__":
    main()
