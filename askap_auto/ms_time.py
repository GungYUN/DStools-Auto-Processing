"""Run inside SIF: earliest finite main-table TIME, respecting MS unit/scale."""
import json
import math
import sys
import numpy as np
from casacore.tables import table
from astropy.time import Time
import astropy.units as u


def read_time(path):
    earliest = None
    with table(str(path), readonly=True, ack=False) as ms:
        metadata = ms.getcolkeywords("TIME")
        units = metadata.get("QuantumUnits", [])
        measure = metadata.get("MEASINFO", {})
        scale = str(measure.get("Ref", "")).lower()
        if len(units) != 1 or scale not in {"utc", "tai", "tt", "tdb", "ut1", "tcg", "tcb"}:
            raise ValueError("MS TIME has missing/unsupported unit or reference scale")
        factor = (1 * u.Unit(units[0])).to_value(u.day)
        for start in range(0, ms.nrows(), 100000):
            values = np.asarray(ms.getcol("TIME", startrow=start, nrow=min(100000, ms.nrows()-start)), dtype=float)
            values = values[np.isfinite(values)]
            if values.size:
                candidate = float(values.min()) * factor
                earliest = candidate if earliest is None else min(earliest, candidate)
    if earliest is None or not math.isfinite(earliest):
        raise ValueError("MS contains no finite TIME")
    return {"mjd": float(Time(earliest, format="mjd", scale=scale).utc.mjd), "scale": "utc", "source": "MS",
            "ms_time_unit": units[0], "ms_time_scale": scale}

if __name__ == "__main__":
    print(json.dumps(read_time(sys.argv[1])))
