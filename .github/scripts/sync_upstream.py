"""Fast-forward the fork's upstream branch and open one review PR; never merge main."""

import argparse
import json
import os
from pathlib import Path
import subprocess
from urllib.parse import urlencode


REPOSITORY = "coledeb/riven"
SOURCE = "rivenmedia/riven"
BODY = """## Upstream Riven updates

This PR tracks the original repository's `main` branch. New upstream commits
are added to this same PR by the daily **Sync upstream** workflow.

Before merging:
- Review changes alongside our TorBox and other fork customizations.
- If GitHub shows **Approve workflows to run**, approve CI and review its results.
- Resolve any merge conflicts and test the resulting app.
- Use **Create a merge commit** to preserve shared upstream history; avoid squash
  or rebase merging these sync PRs.

This automation does not merge the PR or deploy it. After merging, run
`riven-trial/deploy-riven-backend.sh` to update the running stack.
"""


def api(method, endpoint, payload=None):
    command = ["gh", "api", "--method", method, endpoint]
    if payload is not None:
        command += ["--input", "-"]
    result = subprocess.run(
        command, input=json.dumps(payload) if payload is not None else None,
        text=True, capture_output=True, check=True,
    )
    return json.loads(result.stdout)


def sync(request=api, dry_run=False):
    source_sha = request("GET", f"repos/{SOURCE}/git/ref/heads/main")["object"]["sha"]
    current_sha = request("GET", f"repos/{REPOSITORY}/git/ref/heads/upstream")["object"]["sha"]
    if source_sha != current_sha:
        relation = request("GET", f"repos/{REPOSITORY}/compare/{current_sha}...{source_sha}")
        if relation["status"] != "ahead":
            raise RuntimeError("upstream has diverged or is ahead of the source; refusing to overwrite it.")
        if not dry_run:
            # GitHub rejects non-fast-forward changes even if the branch moves
            # between our comparison and update. No force push is permitted.
            request("PATCH", f"repos/{REPOSITORY}/git/refs/heads/upstream",
                    {"sha": source_sha, "force": False})
        print(f"{'Would sync' if dry_run else 'Synced'} upstream to {source_sha}.")

    comparison = request("GET", f"repos/{REPOSITORY}/compare/main...{source_sha}")
    if comparison["ahead_by"] == 0:
        return "Your customized main already contains all upstream commits. No PR needed."

    query = urlencode({"state": "open", "base": "main", "head": "coledeb:upstream"})
    existing = request("GET", f"repos/{REPOSITORY}/pulls?{query}")
    if existing:
        return f"Update PR is tracking upstream: {existing[0]['html_url']}"
    if dry_run:
        return f"Would open an update PR with {comparison['ahead_by']} upstream commits."
    pull = request("POST", f"repos/{REPOSITORY}/pulls", {
        "title": "Sync upstream Riven updates", "base": "main",
        "head": "upstream", "body": BODY,
    })
    return f"Created update PR: {pull['html_url']}"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Inspect GitHub without changing anything")
    args = parser.parse_args()
    result = sync(dry_run=args.dry_run)
    print(result)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a") as summary:
            summary.write(result + "\n")
