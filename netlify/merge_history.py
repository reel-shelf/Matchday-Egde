"""Merge the live site's history.json into the repo copy (live entries win)."""
import json
import sys

repo_path, live_path = sys.argv[1], sys.argv[2]
with open(repo_path) as f:
    repo = json.load(f)
with open(live_path) as f:
    live = json.load(f)
if not isinstance(live.get("matches"), dict):
    sys.exit("live history has no matches")
repo.setdefault("matches", {}).update(live["matches"])
with open(repo_path, "w") as f:
    json.dump(repo, f, separators=(",", ":"))
print(f"Merged live history: {len(repo['matches'])} picks.")
