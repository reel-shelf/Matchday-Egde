import type { Config, Context } from "@netlify/functions"
import { getStore } from "@netlify/blobs"

// Team news from API-Football (api-sports.io): injuries, suspensions, doubts and
// confirmed lineups for games in the next 36 hours. Runs every 5 minutes.
//
// Kick-offs are almost always on a 5-minute mark, so one run always lands in the
// last 5 minutes before kick-off: that run re-checks the lineup and injuries one
// final time. Lineups are polled from 75 minutes before kick-off until they appear
// (clubs usually publish them about 60 minutes before).
//
// Free plan = 100 calls a day, so every call is counted and the function stops at
// API_FOOTBALL_DAILY_LIMIT (default 95). Lineups are checked before anything else.

const API = "https://v3.football.api-sports.io"
const LEAGUE_IDS: Record<string, number> = { PL: 39, PD: 140, SA: 135, BL1: 78, FL1: 61, CL: 2 }
const MIN = 60_000
const LOOK_AHEAD = 36 * 60 * MIN
const LINEUP_FROM = 75 * MIN      // start polling lineups this long before kick-off
const FINAL_CHECK = 6 * MIN       // the run inside this window is the final check
const KEY_PLAYERS_MAX_AGE = 7 * 24 * 60 * MIN

type Player = { id: number; name: string; reason?: string }
type Mapping = { af: number; homeId: number; awayId: number }
type Raw = {
  kickoff: string
  injuries?: { teamId: number; type: string; player: Player }[]
  injuriesAt?: string
  lineups?: { teamId: number; formation: string; xi: Player[] }[]
  lineupsAt?: string
  finalAt?: string
}
type KeyPlayer = { id: number; name: string; teamId: number }
type State = {
  day: string
  calls: number
  dates: Record<string, string>
  map: Record<string, Mapping>
  raw: Record<string, Raw>
  key: Record<string, { at: string; players: KeyPlayer[] }>
  lastError?: string
}
type Match = { id: number; league: string; date: string; home: { name: string }; away: { name: string } }

class OutOfCalls extends Error {}

export default async (_req: Request, context: Context) => {
  const apiKey = Netlify.env.get("API_FOOTBALL_KEY")
  const store = getStore({ name: "team-news", consistency: "strong" })
  if (!apiKey) {
    await store.setJSON("news", { updated: new Date().toISOString(), status: "API_FOOTBALL_KEY is not set", matches: {} })
    return
  }
  const limit = Number(Netlify.env.get("API_FOOTBALL_DAILY_LIMIT") || 95)
  const now = Date.now()
  const today = new Date(now).toISOString().slice(0, 10)

  const state: State = Object.assign(
    { day: today, calls: 0, dates: {}, map: {}, raw: {}, key: {} },
    (await store.get("state", { type: "json" })) ?? {},
  )
  if (state.day !== today) { state.day = today; state.calls = 0 }
  state.lastError = undefined

  // Upcoming games from the latest predictions.
  const siteUrl = context.site?.url || Netlify.env.get("URL")
  const preds = await fetch(`${siteUrl}/data/predictions.json`).then(r => r.ok ? r.json() : null).catch(() => null)
  if (!preds) { console.log("Could not load predictions.json"); return }
  const matches: Match[] = (preds.matches || []).filter((m: Match) => {
    const t = Date.parse(m.date) - now
    return LEAGUE_IDS[m.league] && t > -10 * MIN && t < LOOK_AHEAD
  })
  const keep = new Set(matches.map(m => String(m.id)))
  for (const id of Object.keys(state.raw)) if (!keep.has(id)) delete state.raw[id]
  for (const id of Object.keys(state.map)) if (!keep.has(id)) delete state.map[id]
  for (const d of Object.keys(state.dates)) if (Date.parse(d) < now - 2 * 86400_000) delete state.dates[d]

  const call = async (path: string) => {
    if (state.calls >= limit) throw new OutOfCalls(`Daily limit of ${limit} API-Football calls reached`)
    state.calls++
    const res = await fetch(API + path, { headers: { "x-apisports-key": apiKey } })
    const body: any = await res.json().catch(() => ({}))
    const errs = body.errors && (Array.isArray(body.errors) ? body.errors : Object.values(body.errors))
    if (!res.ok || (errs && errs.length)) throw new Error(`API-Football ${path}: ${errs?.join("; ") || res.status}`)
    return body.response as any[]
  }
  const toKick = (m: Match) => Date.parse(m.date) - now

  try {
    // 1. Find each game's API-Football fixture id (one call per day of games).
    const dates = [...new Set(matches.filter(m => !state.map[m.id]).map(m => m.date.slice(0, 10)))]
    for (const d of dates) {
      if (state.dates[d] && now - Date.parse(state.dates[d]) < 6 * 60 * MIN) continue
      const fixtures = await call(`/fixtures?date=${d}`)
      state.dates[d] = new Date(now).toISOString()
      for (const m of matches.filter(x => x.date.slice(0, 10) === d && !state.map[x.id])) {
        const hit = matchFixture(m, fixtures)
        if (hit) state.map[m.id] = { af: hit.fixture.id, homeId: hit.teams.home.id, awayId: hit.teams.away.id }
      }
    }
    const mapped = matches.filter(m => state.map[m.id])
    const rawOf = (m: Match) => (state.raw[m.id] ??= { kickoff: m.date })

    // 2. Lineups: poll from 75 min out until they appear, then once more in the last 5 minutes.
    const lineupDue = mapped.filter(m => {
      const t = toKick(m), r = rawOf(m)
      if (t > LINEUP_FROM || t < -2 * MIN) return false
      return !r.lineups?.length || (t <= FINAL_CHECK && !r.finalAt)
    })
    for (const batch of chunks(lineupDue, 20)) {
      const res = await call(`/fixtures?ids=${batch.map(m => state.map[m.id].af).join("-")}`)
      for (const m of batch) {
        const fx = res.find(f => f.fixture?.id === state.map[m.id].af)
        const r = rawOf(m)
        r.lineupsAt = new Date(now).toISOString()
        if (toKick(m) <= FINAL_CHECK) r.finalAt = r.lineupsAt
        if (fx?.lineups?.length) {
          r.lineups = fx.lineups.map((l: any) => ({
            teamId: l.team?.id, formation: l.formation || "",
            xi: (l.startXI || []).map((p: any) => ({ id: p.player?.id, name: p.player?.name })),
          }))
        }
      }
    }

    // 3. Injuries & suspensions: every 4 hours, every 30 min in the last 3 hours, and at the final check.
    const injuryDue = mapped.filter(m => {
      const t = toKick(m), r = rawOf(m)
      if (t < -2 * MIN) return false
      if (!r.injuriesAt) return true
      const age = now - Date.parse(r.injuriesAt)
      if (t <= FINAL_CHECK && r.finalAt && Date.parse(r.injuriesAt) < Date.parse(r.finalAt)) return true
      return age > (t <= 3 * 60 * MIN ? 30 * MIN : 4 * 60 * MIN)
    })
    for (const batch of chunks(injuryDue, 20)) {
      const res = await call(`/injuries?ids=${batch.map(m => state.map[m.id].af).join("-")}`)
      for (const m of batch) {
        const r = rawOf(m)
        r.injuriesAt = new Date(now).toISOString()
        r.injuries = res.filter(x => x.fixture?.id === state.map[m.id].af).map(x => ({
          teamId: x.team?.id, type: x.player?.type || "",
          player: { id: x.player?.id, name: x.player?.name, reason: x.player?.reason || "" },
        }))
      }
    }

    // 4. Key players (top scorers and assisters), one league per run, refreshed weekly.
    if (limit - state.calls >= 20) {
      const stale = [...new Set(matches.map(m => m.league))]
        .find(c => !state.key[c] || now - Date.parse(state.key[c].at) > KEY_PLAYERS_MAX_AGE)
      if (stale) {
        const kick = new Date(matches.find(m => m.league === stale)!.date)
        const season = kick.getUTCMonth() >= 6 ? kick.getUTCFullYear() : kick.getUTCFullYear() - 1
        const players: KeyPlayer[] = []
        for (const kind of ["topscorers", "topassists"]) {
          for (const x of await call(`/players/${kind}?league=${LEAGUE_IDS[stale]}&season=${season}`)) {
            const teamId = x.statistics?.[0]?.team?.id
            if (x.player?.id && teamId && !players.some(p => p.id === x.player.id))
              players.push({ id: x.player.id, name: x.player.name, teamId })
          }
        }
        state.key[stale] = { at: new Date(now).toISOString(), players }
      }
    }
  } catch (e) {
    state.lastError = (e as Error).message
    console.log(state.lastError)
  }

  // Build what the app shows.
  const news: Record<string, unknown> = {}
  for (const m of matches) {
    const map = state.map[m.id], r = state.raw[m.id]
    if (!map || !r || (!r.injuries && !r.lineups)) continue
    const keyIds = new Set((state.key[m.league]?.players || []).map(p => p.id))
    const side = (teamId: number) => {
      const lineup = r.lineups?.find(l => l.teamId === teamId)
      const inXI = new Set((lineup?.xi || []).map(p => p.id))
      const out: (Player & { status: string; key: boolean })[] = []
      for (const i of (r.injuries || []).filter(x => x.teamId === teamId)) {
        if (inXI.has(i.player.id)) continue // was a doubt, but starts
        const doubt = /questionable/i.test(i.type) && !lineup
        out.push({ ...i.player, status: doubt ? "doubt" : "out", key: keyIds.has(i.player.id) })
      }
      const benched = lineup
        ? (state.key[m.league]?.players || []).filter(p => p.teamId === teamId && !inXI.has(p.id)
            && !out.some(o => o.id === p.id)).map(p => ({ id: p.id, name: p.name }))
        : []
      return { out, benched, formation: lineup?.formation || null, xi: lineup ? lineup.xi.map(p => p.name) : null }
    }
    news[m.id] = {
      kickoff: m.date,
      home: side(map.homeId),
      away: side(map.awayId),
      lineups: !!r.lineups?.length,
      finalCheck: r.finalAt || null,
      injuriesChecked: r.injuriesAt || null,
      lineupsChecked: r.lineupsAt || null,
    }
  }
  await store.setJSON("state", state)
  await store.setJSON("news", {
    updated: new Date(now).toISOString(),
    status: state.lastError || "ok",
    callsToday: state.calls,
    limit,
    matches: news,
  })
}

function chunks<T>(xs: T[], n: number): T[][] {
  const out: T[][] = []
  for (let i = 0; i < xs.length; i += n) out.push(xs.slice(i, i + n))
  return out
}

const STOP = new Set(["fc", "cf", "ac", "afc", "sc", "ssc", "as", "us", "rc", "rcd", "ca", "cd", "ud", "sd", "club",
  "de", "del", "la", "le", "calcio", "1", "tsg", "vfb", "vfl", "sv", "bv", "fk", "sk", "if", "bk", "and", "the", "hotspur"])
function tokens(name: string): Set<string> {
  return new Set(name.normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase()
    .replace(/[^a-z0-9 ]/g, " ").split(/\s+/).filter(w => w && !STOP.has(w)))
}
function similar(a: string, b: string): number {
  const x = tokens(a), y = tokens(b)
  let n = 0
  for (const w of x) if (y.has(w) || [...y].some(v => v.length > 3 && w.length > 3 && (v.startsWith(w) || w.startsWith(v)))) n++
  return n / Math.max(1, Math.min(x.size, y.size))
}
// Same competition, kick-off within 3 hours, best team-name match.
function matchFixture(m: Match, fixtures: any[]) {
  const t = Date.parse(m.date)
  let best: any = null, bestScore = 0
  for (const f of fixtures) {
    if (f.league?.id !== LEAGUE_IDS[m.league]) continue
    if (Math.abs(f.fixture.timestamp * 1000 - t) > 3 * 60 * MIN) continue
    const score = similar(m.home.name, f.teams.home.name) + similar(m.away.name, f.teams.away.name)
      + (f.fixture.timestamp * 1000 === t ? 0.5 : 0)
    if (score > bestScore) { best = f; bestScore = score }
  }
  return bestScore >= 1 ? best : null
}

export const config: Config = {
  schedule: "*/5 * * * *", // every 5 minutes (UTC)
}
