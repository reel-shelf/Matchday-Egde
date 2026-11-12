#!/bin/sh
# Netlify build: refresh predictions, then copy the app into dist/.
set -e

# history.json holds every pick locked in before kick-off. Builds don't commit back to
# GitHub, so start from the copy on the live site and merge it with the repo's copy.
if [ -n "$URL" ] && curl -fsS --max-time 30 "$URL/data/history.json" -o /tmp/live-history.json; then
  python3 netlify/merge_history.py data/history.json /tmp/live-history.json || echo "Could not merge live history; using the repo copy."
else
  echo "No live history.json yet; using the repo copy."
fi

if [ -n "$FOOTBALL_DATA_TOKEN" ]; then
  python3 predict.py
else
  echo "FOOTBALL_DATA_TOKEN is not set: publishing the predictions already in data/."
fi

rm -rf dist && mkdir dist
cp index.html sw.js manifest.json icon-192.png icon-512.png apple-touch-icon.png dist/
cp -r data dist/
