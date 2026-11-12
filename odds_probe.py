import json, urllib.request
from datetime import datetime, timezone
day = datetime.now(timezone.utc).strftime("%Y%m%d")
url = f"https://site.api.espn.com/apis/site/v2/sports/soccer/uefa.nations/scoreboard?dates={day}"
ev = json.load(urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})))["events"]
o = dict(ev[0]["competitions"][0]["odds"][0]); o.pop("link", None)
for k in ("provider",): o.pop(k, None)
print(ev[0]["name"]); print(json.dumps(o, indent=1)[:4000])
