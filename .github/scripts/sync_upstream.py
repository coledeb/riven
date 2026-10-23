"""Fast-forward the fork's upstream branch and open one review PR; never merge main."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import time
from urllib.parse import urlencode


REPOSITORY = "coledeb/riven"
SOURCE = "rivenmedia/riven"
STATUS_CONTEXT = "Upstream sync checks"
BODY = """## Upstream Riven updates

This PR tracks the original repository's `main` branch. New upstream commits
are added to this same PR by the daily **Sync upstream** workflow.

Before merging:
- Review changes alongside our TorBox and other fork customizations.
- Check the **Upstream sync checks** status. The workflow tests this PR merged
  into main, and reruns those checks whenever upstream adds commits.
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
        return "Your customized main already contains all upstream commits. No PR needed.", None, None

    query = urlencode({"state": "open", "base": "main", "head": "coledeb:upstream"})
    existing = request("GET", f"repos/{REPOSITORY}/pulls?{query}")
    if existing:
        return f"Update PR is tracking upstream: {existing[0]['html_url']}", existing[0]["number"], source_sha
    if dry_run:
        return f"Would open an update PR with {comparison['ahead_by']} upstream commits.", None, None
    pull = request("POST", f"repos/{REPOSITORY}/pulls", {
        "title": "Sync upstream Riven updates", "base": "main",
        "head": "upstream", "body": BODY,
    })
    return f"Created update PR: {pull['html_url']}", pull["number"], source_sha


def merge_commit(request, number, head_sha, attempts=24, wait=5):
    """Return GitHub's test merge of the PR into main, or None if it conflicts."""
    for _ in range(attempts):
        pull = request("GET", f"repos/{REPOSITORY}/pulls/{number}")
        if pull["head"]["sha"] == head_sha and pull["mergeable"] is not None:
            return pull["merge_commit_sha"] if pull["mergeable"] else None
        time.sleep(wait)
    raise RuntimeError("GitHub did not compute the PR's test merge in time.")


def plan_checks(request, number, head_sha, force=False):
    """Pick the commit for the checks job; skip heads that already have a result."""
    statuses = request("GET", f"repos/{REPOSITORY}/commits/{head_sha}/status")["statuses"]
    if not force and any(status["context"] == STATUS_CONTEXT for status in statuses):
        return {}
    merge_sha = merge_commit(request, number, head_sha)
    if merge_sha is None:
        request("POST", f"repos/{REPOSITORY}/statuses/{head_sha}", {
            "state": "failure", "context": STATUS_CONTEXT,
            "description": "Merge conflicts with main; resolve them before merging.",
        })
        return {}
    return {"head_sha": head_sha, "check_sha": merge_sha}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Inspect GitHub without changing anything")
    args = parser.parse_args()
    result, number, head_sha = sync(dry_run=args.dry_run)
    print(result)
    if number and not args.dry_run:
        outputs = plan_checks(api, number, head_sha,
                              force=os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch")
        result += " Checks queued." if outputs else " Checks already reported for this commit."
        if os.environ.get("GITHUB_OUTPUT"):
            with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
                output.writelines(f"{key}={value}\n" for key, value in outputs.items())
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a") as summary:
            summary.write(result + "\n")
