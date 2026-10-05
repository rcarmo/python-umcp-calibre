#!/usr/bin/env python3
"""Run pre-release tests with CPU/allocation profiling, report, then clean up."""
import cProfile
import os
from pathlib import Path
import platform
import pstats
import shutil
import subprocess
import sys
import tracemalloc
import unittest

out = Path(os.environ["PROFILE_DIR"])
out.mkdir(parents=True, exist_ok=False)
profile = cProfile.Profile()
tracemalloc.start(25)
try:
    profile.enable()
    suite = unittest.defaultTestLoader.discover("tests")
    result = unittest.TextTestRunner(verbosity=2, warnings="error").run(suite)
    profile.disable()
    snapshot = tracemalloc.take_snapshot()
    profile.dump_stats(out / "cpu.prof")
    snapshot.dump(out / "allocations.snapshot")

    print("\nPre-release profiling summary")
    print(f"revision={subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()}")
    print(f"python={sys.version.split()[0]} platform={platform.platform()}")
    print(f"tests={result.testsRun} failures={len(result.failures)} errors={len(result.errors)}")
    print("\nCPU cumulative top 20:")
    pstats.Stats(profile, stream=sys.stdout).strip_dirs().sort_stats("cumulative").print_stats(20)
    print("Allocation top 20:")
    for stat in snapshot.statistics("lineno")[:20]:
        print(stat)
    raise SystemExit(not result.wasSuccessful())
finally:
    tracemalloc.stop()
    shutil.rmtree(out, ignore_errors=True)
