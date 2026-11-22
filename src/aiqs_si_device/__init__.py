"""Public stub of the proprietary device package.

The production aiqs_si_device package (the dedicated-device /
diode layer) is proprietary and is distributed only to the
dedicated PC. This package contains only a deterministic synthetic
device stub so the open-source shell can run end to end in
synthetic mode. It performs no resonance computation: scan results
are marked score_kind="stub" and carry no real ranking.
"""
