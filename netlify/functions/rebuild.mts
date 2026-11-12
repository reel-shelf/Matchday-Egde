import type { Config } from "@netlify/functions"

// Starts a fresh Netlify build (which runs predict.py) twice a day.
// Needs BUILD_HOOK_URL: Project configuration > Build & deploy > Build hooks.
export default async () => {
  const hook = Netlify.env.get("BUILD_HOOK_URL")
  if (!hook) {
    console.log("BUILD_HOOK_URL is not set, so predictions can't refresh on a schedule.")
    return
  }
  const res = await fetch(hook, { method: "POST" })
  console.log(`Build hook answered ${res.status}`)
}

export const config: Config = {
  schedule: "17 11,23 * * *", // 11:17 and 23:17 UTC, same as the old GitHub Action
}
