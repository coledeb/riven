"""Compare check error counts between main and a sync PR's test merge.

Upstream's own code doesn't pass these checks, so the sync PR fails only when
merging upstream adds errors that main doesn't already have.
"""

import json
import os
from pathlib import Path
import subprocess
import sys


def run(directory, *command):
    return subprocess.run(command, cwd=directory, text=True, capture_output=True)


def setup(directory, *command):
    result = run(directory, *command)
    if result.returncode:
        sys.exit(f"{' '.join(command)} failed in {directory}:\n{result.stdout}{result.stderr}")


def pyright_errors(directory):
    output = run(directory, "npx", "--yes", "pyright@1", "--outputjson",
                 "--pythonpath", str(Path(directory, ".venv/bin/python").resolve())).stdout
    return json.loads(output)["summary"]["errorCount"]


def count(directory):
    setup(directory, "uv", "sync", "--locked", "--all-extras", "--dev", "--quiet")
    return {"pyright errors": pyright_errors(directory)}


if __name__ == "__main__":
    base, pull = count(sys.argv[1]), count(sys.argv[2])
    rows = [f"| {name} | {base[name]} | {pull[name]} |" for name in base]
    added = {name: pull[name] - base[name] for name in base if pull[name] > base[name]}
    if added:
        result = "New: " + ", ".join(f"+{n} {name}" for name, n in added.items())
    else:
        result = "No new errors vs main"
    print(result, *rows, sep="\n")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a") as summary:
            summary.write(f"{result}\n\n| Check | main | with upstream |\n| --- | --- | --- |\n")
            summary.write("\n".join(rows) + "\n")
    if os.environ.get("GITHUB_OUTPUT"):
        with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
            output.write(f"result={result}\n")
    sys.exit(1 if added else 0)
