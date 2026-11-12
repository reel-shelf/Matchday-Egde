import type { Config } from "@netlify/functions"
import { getStore } from "@netlify/blobs"

// Serves the latest team news that team-news.mts saved.
export default async () => {
  const news = await getStore({ name: "team-news", consistency: "strong" }).get("news", { type: "json" })
  return Response.json(news ?? { updated: null, matches: {} }, {
    headers: { "Cache-Control": "public, max-age=0, must-revalidate" },
  })
}

export const config: Config = { path: "/api/team-news" }
