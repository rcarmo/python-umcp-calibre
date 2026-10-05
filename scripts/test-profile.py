#!/usr/bin/env python3
"""Run the unittest suite with retained CPU and allocation profiles."""
import cProfile
import os
from pathlib import Path
import platform
import pstats
import subprocess
import sys
import tracemalloc
import unittest

out = Path(os.environ["PROFILE_DIR"])
out.mkdir(parents=True, exist_ok=False)
(out / "environment.txt").write_text(
    f"revision={subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()}\n"
    f"python={sys.version}\nplatform={platform.platform()}\n"
    f"tmpdir={os.environ.get('TMPDIR', '')}\n"
)
profile = cProfile.Profile()
tracemalloc.start(25)
profile.enable()
suite = unittest.defaultTestLoader.discover("tests")
result = unittest.TextTestRunner(verbosity=2, warnings="error").run(suite)
profile.disable()
snapshot = tracemalloc.take_snapshot()
profile.dump_stats(out / "cpu.prof")
snapshot.dump(out / "allocations.snapshot")
with (out / "cpu-top.txt").open("w") as stream:
    pstats.Stats(profile, stream=stream).strip_dirs().sort_stats("cumulative").print_stats(40)
with (out / "allocations-top.txt").open("w") as stream:
    for stat in snapshot.statistics("lineno")[:40]:
        stream.write(f"{stat}\n")
(out / "result.txt").write_text(
    f"run={result.testsRun}\nfailures={len(result.failures)}\nerrors={len(result.errors)}\n"
)
raise SystemExit(not result.wasSuccessful())
