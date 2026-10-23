#!/usr/bin/env python3
"""Compile and run C++ host fixtures with synthetic installation settings; no hardware."""
from pathlib import Path
import subprocess
import tempfile
root = Path(__file__).resolve().parent
suites = ['test_protocol', 'test_bench_policy', 'test_boot_diagnostics', 'test_sd_archive']
with tempfile.TemporaryDirectory(prefix="meter-host-tests-") as build:
    for name in suites:
        command = ["g++", "-std=c++17", "-O2", "-Wall", "-Wextra", "-DMETER_HOST_TEST=1", 
                   "-I" + str(root / "tests/host_stubs"), "-I" + str(root),
                   str(root / (name + ".cpp")), "-o", str(Path(build) / name)]
        subprocess.run(command, check=True)
        subprocess.run([str(Path(build) / name)], check=True)
print(f"Passed {len(suites)} C++ host suites; no hardware reliability claim.")
