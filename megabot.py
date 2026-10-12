import os
import time
import certifi
import discord
import requests
import asyncio

# ==========================================
# PARCHE SSL
# ==========================================
os.environ["SSL_CERT_FILE"] = certifi.where()

# ==========================================
# CONFIGURACION (variables de entorno)
# ==========================================
DISCORD_TOKEN    = os.getenv("DISCORD_TOKEN")
API_FOOTBALL_KEY = os.getenv("API_FOOTBALL_KEY")


def _int_env(name):
    try:
        return int(os.getenv(name, "0"))
    except (TypeError, ValueError):
        return 0


CHANNEL_ROJAS_ID    = _int_env("CHANNEL_ROJAS_ID")
CHANNEL_XG_ID       = _int_env("CHANNEL_XG_ID")
CHANNEL_MISMATCH_ID = _int_env("CHANNEL_MISMATCH_ID")

# ==========================================
# ENDPOINTS API-FOOTBALL
# ==========================================
API_FIXTURES_URL   = "https://v3.football.api-sports.io/fixtures"
API_STATS_URL      = "https://v3.football.api-sports.io/fixtures/statistics"
API_TEAM_STATS_URL = "https://v3.football.api-sports.io/teams/statistics"

# ==========================================
# CONSTANTES DE POLLING Y CACHE
# ==========================================
FAST_POLL          = 20      # fixtures + tarjetas rojas
SLOW_POLL          = 120     # xG strategies
FIXTURES_CACHE_TTL = FAST_POLL
XG_CACHE_TTL       = 110
TEAM_CACHE_TTL     = 86400   # 24 h
STORM_MIN_MINUTE   = 60
STORM_MIN_XG       = 1.2
STORM_MIN_HIST_AVG = 1.8
XG_MAX_CONCURRENCY = 8

# ==========================================
# ESTADO GLOBAL COMPARTIDO
# ==========================================
state = {
    "seen_red_cards": set(),
    "xg_previous": {},
    "momentum_alerts_sent": set(),
    "storm_alerts_sent": set(),
    "mismatch_alerts_sent": set(),
    "fixtures_cache": [],
    "fixtures_cache_ts": 0.0,
    "xg_stats_cache": {},
    "team_stats_cache": {},
    "momentum_threshold": 0.15,
    "mismatch_threshold": 1.5,
    "rojas_activo": True,
    "xg_activo": True,
    "mismatch_activo": True,
    "first_run_rojas": True,
    "cache_hits_fixtures": 0,
    "cache_miss_fixtures": 0,
}

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)


# ==========================================
# CEREBRO DE FILTRADO (V5.2 - Lista Blanca VIP)
# Copiado sin cambios desde bot-rojas
# ==========================================
def es_partido_importante(league_name, country_name, home_team, away_team):
    league_lower  = (league_name or "").lower()
    country_lower = (country_name or "").lower()
    teams_lower   = ((home_team or "") + " " + (away_team or "")).lower()

    # ----- 0. Lista Blanca VIP -----
    vip_keywords = [
        "regionalliga", "regional liga",
        "ekstraklasa",
        "division profesional",
        "2. bundesliga", "serie b",
        "ligue 2", "championship",
        "segunda division", "la liga 2",
        "eerste divisie", "2nd division",
    ]
    if any(vip in league_lower for vip in vip_keywords):
        return True
    if country_lower == "bolivia" and ("profesional" in league_lower or "primera" in league_lower):
        return True

    # ----- 1. Juvenil y reservas -----
    bloqueados_juvenil = [
        "u17", "u18", "u23",
        "sub-17", "sub-23",
        "reserve", "reserves", "league cup",
        "campiconato", "belofte",
        "premier league 2",
    ]
    if any(x in league_lower for x in bloqueados_juvenil):
        return False
    if any(x in teams_lower for x in [" ii", " b "]):
        return False

    # ----- 2. Femenino (con paises VIP) -----
    tiene_w_aislada = " w " in league_lower or league_lower.endswith(" w") or "(w)" in league_lower
    keywords_fem = [
        "women", "femenino", "femenina", "ladies",
        "frauen", "dames", "feminin", "femminile",
        "w-league", "girls", "wsl", "kvinne", "kvinde", "vrouwen",
    ]
    es_femenino = any(x in league_lower for x in keywords_fem) or tiene_w_aislada
    if es_femenino:
        paises_fem_permitidos = [
            "Germany", "Netherlands", "Italy", "Saudi Arabia",
            "Spain", "USA", "Brazil", "Austria", "France",
            "Denmark", "Norway", "England",
        ]
        if country_name not in paises_fem_permitidos:
            return False
        return True

    # ----- 3. Bloqueo Asia/Oceania (masculino) -----
    blocked_countries = [
        "australia", "japan", "china", "korea", "saudi arabia", "qatar",
        "united arab emirates", "iran", "thailand", "vietnam", "indonesia",
        "malaysia", "new zealand", "india", "uzbekistan", "iraq", "jordan",
        "kuwait", "lebanon", "oman", "syria", "bahrain", "singapore",
        "hong kong", "philippines", "yemen", "palestine", "tajikistan",
        "turkmenistan", "kyrgyzstan", "myanmar", "cambodia",
    ]
    if any(c in country_lower for c in blocked_countries):
        return False

    blocked_leagues_keywords = [
        "j1 league", "j2 league", "k league", "saudi pro", "stars league",
        "pro league", "a-league", "chinese super league", "afc champions",
        "asian cup", "gulf cup", "isl", "indian super", "thaileague",
    ]
    if any(l in league_lower for l in blocked_leagues_keywords):
        return False

    # ----- 4. Divisiones bajas -----
    bloqueados_bajos = [
        "3rd", "4th", "5th", "non-league", "amateur", "promotion",
        "tweede divisie", "derde divisie", "vierde divisie",
        "oberliga", "landesliga",
        "serie c", "serie d", "eccellenza",
        "national 2", "national 3",
        "segunda rfef", "tercera rfef",
        "liga 3", "campeonato de portugal",
        "division 3", "federation",
    ]
    if any(x in league_lower for x in bloqueados_bajos):
        return False

    return True


# ==========================================
# LLAMADAS API (sync, ejecutadas via asyncio.to_thread)
# ==========================================
def _headers():
    return {"x-apisports-key": API_FOOTBALL_KEY, "Accept": "application/json"}


def get_live_fixtures():
    try:
        r = requests.get(API_FIXTURES_URL, headers=_headers(), params={"live": "all"}, timeout=10)
        r.raise_for_status()
        return r.json().get("response", [])
    except Exception as e:
        print(f"[API] fixtures error: {e}")
        return []


def get_fixture_stats(fixture_id):
    try:
        r = requests.get(API_STATS_URL, headers=_headers(),
                         params={"fixture": fixture_id}, timeout=10)
        r.raise_for_status()
        data = r.json().get("response", [])
        if not data:
            return None
        xg_home, xg_away = 0.0, 0.0
        home_id = data[0]["team"]["id"]
        for team_data in data:
            for stat in team_data.get("statistics", []):
                if stat.get("type") == "expected_goals" and stat.get("value") is not None:
                    try:
                        v = float(stat["value"])
                    except (TypeError, ValueError):
                        v = 0.0
                    if team_data["team"]["id"] == home_id:
                        xg_home = v
                    else:
                        xg_away = v
        return {"home_xg": xg_home, "away_xg": xg_away}
    except Exception as e:
        print(f"[API] xg error fixture={fixture_id}: {e}")
        return None


def get_team_stats(league_id, season, team_id):
    key = f"{team_id}-{league_id}-{season}"
    now = time.time()
    cached = state["team_stats_cache"].get(key)
    if cached and now - cached.get("fetched_at", 0) < TEAM_CACHE_TTL:
        return cached
    try:
        r = requests.get(API_TEAM_STATS_URL, headers=_headers(),
                         params={"league": league_id, "season": season, "team": team_id},
                         timeout=10)
        r.raise_for_status()
        resp = r.json().get("response") or {}
        gf = ga = 0.0
        goals = resp.get("goals") if isinstance(resp, dict) else None
        if goals:
            gf_str = (goals.get("for") or {}).get("average", {}).get("total")
            ga_str = (goals.get("against") or {}).get("average", {}).get("total")
            gf = float(gf_str) if gf_str is not None else 0.0
            ga = float(ga_str) if ga_str is not None else 0.0
        entry = {"gf": gf, "ga": ga, "net": round(gf - ga, 2), "fetched_at": now}
        state["team_stats_cache"][key] = entry
        return entry
    except Exception as e:
        print(f"[API] team stats error team={team_id}: {e}")
        return {"gf": 0.0, "ga": 0.0, "net": 0.0, "fetched_at": now}


# ==========================================
# WRAPPERS ASINCRONOS CON CACHE COMPARTIDA
# ==========================================
async def fetch_fixtures_cached():
    now = time.time()
    if state["fixtures_cache"] and now - state["fixtures_cache_ts"] < FIXTURES_CACHE_TTL:
        state["cache_hits_fixtures"] += 1
        print(f"[CACHE] fixtures HIT ({len(state['fixtures_cache'])} partidos)")
        return state["fixtures_cache"]
    data = await asyncio.to_thread(get_live_fixtures)
    state["fixtures_cache"]    = data
    state["fixtures_cache_ts"] = now
    state["cache_miss_fixtures"] += 1
    print(f"[CACHE] fixtures MISS -> refresh ({len(data)} partidos)")
    return data


async def get_xg_cached(fixture_id):
    now = time.time()
    entry = state["xg_stats_cache"].get(fixture_id)
    if entry and now - entry.get("fetched_at", 0) < XG_CACHE_TTL:
        return entry
    data = await asyncio.to_thread(get_fixture_stats, fixture_id)
    if data is None:
        return None
    entry = {**data, "fetched_at": now}
    state["xg_stats_cache"][fixture_id] = entry
    return entry


async def enrich_with_xg(fixtures):
    sem = asyncio.Semaphore(XG_MAX_CONCURRENCY)

    async def one(fixture):
        async with sem:
            xg = await get_xg_cached(fixture["fixture"]["id"])
            return fixture, xg

    results = await asyncio.gather(*(one(f) for f in fixtures), return_exceptions=True)
    enriched = []
    for res in results:
        if isinstance(res, Exception):
            print(f"[SLOW] xg fetch exception: {res}")
            continue
        fixture, xg = res
        if xg is None:
            continue
        enriched.append((fixture, xg))
    return enriched


# ==========================================
# DETECTORES
# ==========================================
def detect_red_cards(fixtures, is_startup=False):
    alerts = []
    for item in fixtures:
        league  = item["league"]["name"]
        country = item["league"]["country"]
        home    = item["teams"]["home"]["name"]
        away    = item["teams"]["away"]["name"]
        if not es_partido_importante(league, country, home, away):
            continue

        fixture_id = item["fixture"]["id"]
        gh = item["goals"]["home"]
        ga = item["goals"]["away"]

        for ev in item.get("events") or []:
            if ev.get("type") != "Card":
                continue
            detail = ev.get("detail", "")
            es_roja           = "Red" in detail
            es_doble_amarilla = "Second" in detail and "Yellow" in detail
            if not es_roja and not es_doble_amarilla:
                continue

            minute = ev["time"]["elapsed"]
            extra  = ev["time"]["extra"]
            team   = ev["team"]["name"]
            player = (ev.get("player") or {}).get("name") or "Jugador"

            key = f"{fixture_id}-{minute}-{player}-{detail}"
            if key in state["seen_red_cards"]:
                continue
            state["seen_red_cards"].add(key)
            if is_startup:
                continue

            time_str = f"{minute}+{extra}'" if extra else f"{minute}'"
            titulo = "TARJETA ROJA DIRECTA" if es_roja else "DOBLE AMARILLA (EXPULSIÓN)"
            emoji  = "🟥" if es_roja else "🟨🟥"
            alerts.append(
                f"{emoji} **{titulo}**\n"
                f"🏆 {league} ({country})\n"
                f"👤 **{player}** ({team})\n"
                f"⏱️ Min {time_str} | {home} `{gh}-{ga}` {away}"
            )
    return alerts


def detect_momentum(fixture, xg, is_startup=False):
    fixture_id = fixture["fixture"]["id"]
    minute     = fixture["fixture"]["status"]["elapsed"]
    if minute is None or minute < 5:
        return None

    home_name = fixture["teams"]["home"]["name"]
    away_name = fixture["teams"]["away"]["name"]
    gh = fixture["goals"]["home"]
    ga = fixture["goals"]["away"]
    league_name = fixture["league"]["name"]
    country     = fixture["league"]["country"]
    xg_home = xg["home_xg"]
    xg_away = xg["away_xg"]

    prev = state["xg_previous"].get(fixture_id)
    state["xg_previous"][fixture_id] = {
        "min": minute, "xg_home": xg_home, "xg_away": xg_away,
        "home": home_name, "away": away_name, "gh": gh, "ga": ga,
    }
    if prev is None or is_startup:
        return None

    diff_home = xg_home - prev["xg_home"]
    diff_away = xg_away - prev["xg_away"]
    min_diff  = minute - prev["min"]
    if min_diff <= 0:
        return None

    threshold = state["momentum_threshold"]
    equipo, salto = None, 0.0
    if diff_home >= threshold and diff_home >= diff_away:
        equipo, salto = home_name, diff_home
    elif diff_away >= threshold:
        equipo, salto = away_name, diff_away
    if not equipo:
        return None

    alert_key = f"{fixture_id}-{minute}-{equipo}"
    if alert_key in state["momentum_alerts_sent"]:
        return None
    state["momentum_alerts_sent"].add(alert_key)

    return (
        f"📈 **ALERTA DE MOMENTUM** 📈\n"
        f"⚠️ **{equipo}** está atacando con todo.\n"
        f"🏆 {league_name} ({country})\n"
        f"🏟️ {home_name} `{gh}` - `{ga}` {away_name} (Min {minute}')\n"
        f"📊 **Salto xG:** `+{round(salto, 2)}` en {min_diff} mins."
    )


async def detect_tormenta_perfecta(fixture, xg):
    fixture_id = fixture["fixture"]["id"]
    minute = fixture["fixture"]["status"]["elapsed"]
    if minute is None or minute < STORM_MIN_MINUTE:
        return []

    league_name = fixture["league"]["name"]
    country     = fixture["league"]["country"]
    league_id   = fixture["league"]["id"]
    season      = fixture["league"]["season"]
    home_id     = fixture["teams"]["home"]["id"]
    away_id     = fixture["teams"]["away"]["id"]
    home_name   = fixture["teams"]["home"]["name"]
    away_name   = fixture["teams"]["away"]["name"]
    gh = fixture["goals"]["home"]
    ga = fixture["goals"]["away"]
    xg_home = xg["home_xg"]
    xg_away = xg["away_xg"]

    alerts = []

    if gh == 0 and xg_home >= STORM_MIN_XG:
        key = f"{fixture_id}-home"
        if key not in state["storm_alerts_sent"]:
            stats = await asyncio.to_thread(get_team_stats, league_id, season, home_id)
            avg = stats["gf"]
            if avg >= STORM_MIN_HIST_AVG:
                state["storm_alerts_sent"].add(key)
                alerts.append(
                    f"⛈️ **¡TORMENTA PERFECTA (GOL INMINENTE)!** ⛈️\n"
                    f"⚠️ **{home_name}** tiene 0 goles hoy, pero es una máquina de anotar.\n"
                    f"🏆 {league_name} ({country}) - Min `{minute}'`\n"
                    f"🏟️ {home_name} `{gh}` - `{ga}` {away_name}\n\n"
                    f"📊 **xG Generado Hoy:** `{xg_home}`\n"
                    f"📖 **Historial:** Promedian `{avg}` goles por partido en la temporada.\n"
                    f"💡 *Altísima probabilidad de gol local. ¡Atento a la cuota!*"
                )

    if ga == 0 and xg_away >= STORM_MIN_XG:
        key = f"{fixture_id}-away"
        if key not in state["storm_alerts_sent"]:
            stats = await asyncio.to_thread(get_team_stats, league_id, season, away_id)
            avg = stats["gf"]
            if avg >= STORM_MIN_HIST_AVG:
                state["storm_alerts_sent"].add(key)
                alerts.append(
                    f"⛈️ **¡TORMENTA PERFECTA (GOL INMINENTE)!** ⛈️\n"
                    f"⚠️ **{away_name}** tiene 0 goles hoy, pero es una máquina de anotar.\n"
                    f"🏆 {league_name} ({country}) - Min `{minute}'`\n"
                    f"🏟️ {home_name} `{gh}` - `{ga}` {away_name}\n\n"
                    f"📊 **xG Generado Hoy:** `{xg_away}`\n"
                    f"📖 **Historial:** Promedian `{avg}` goles por partido en la temporada.\n"
                    f"💡 *Altísima probabilidad de gol visitante. ¡Atento a la cuota!*"
                )

    return alerts


async def detect_mismatch(fixture):
    fixture_id = fixture["fixture"]["id"]
    if fixture_id in state["mismatch_alerts_sent"]:
        return None
    minute = fixture["fixture"]["status"]["elapsed"]
    if minute is None or minute < 5:
        return None

    league_name = fixture["league"]["name"]
    country     = fixture["league"]["country"]
    league_id   = fixture["league"]["id"]
    season      = fixture["league"]["season"]
    home_id     = fixture["teams"]["home"]["id"]
    away_id     = fixture["teams"]["away"]["id"]
    home_name   = fixture["teams"]["home"]["name"]
    away_name   = fixture["teams"]["away"]["name"]
    gh = fixture["goals"]["home"]
    ga = fixture["goals"]["away"]

    stats_home = await asyncio.to_thread(get_team_stats, league_id, season, home_id)
    stats_away = await asyncio.to_thread(get_team_stats, league_id, season, away_id)
    if not ((stats_home["gf"] or stats_home["ga"]) and (stats_away["gf"] or stats_away["ga"])):
        return None

    brecha = abs(stats_home["net"] - stats_away["net"])
    if brecha < state["mismatch_threshold"]:
        return None

    if stats_home["net"] > stats_away["net"]:
        fav, deb, sf, sd = home_name, away_name, stats_home, stats_away
    else:
        fav, deb, sf, sd = away_name, home_name, stats_away, stats_home

    state["mismatch_alerts_sent"].add(fixture_id)
    return (
        f"⚖️ **ALERTA: GRAN DESIGUALDAD HISTÓRICA** ⚖️\n"
        f"⚠️ Diferencia masiva en el rendimiento de temporada.\n"
        f"🏆 {league_name} ({country}) - Min `{minute}'`\n"
        f"🏟️ {home_name} `{gh}` - `{ga}` {away_name}\n\n"
        f"🟢 **{fav}:** Anota `{sf['gf']}` | Recibe `{sf['ga']}` (Neto: `{sf['net']}`)\n"
        f"🔴 **{deb}:** Anota `{sd['gf']}` | Recibe `{sd['ga']}` (Neto: `{sd['net']}`)\n"
        f"📊 **Brecha de Poder:** `{round(brecha, 2)}` goles de ventaja estadística.\n"
        f"💡 *Oportunidad para apostar a goles del favorito o Hándicap.*"
    )


# ==========================================
# GC DE MEMORIA (partidos que ya terminaron)
# ==========================================
def _prune_prefixed_set(s, live_ids):
    keep = set()
    for k in s:
        try:
            fid = int(str(k).split("-", 1)[0])
        except ValueError:
            keep.add(k)
            continue
        if fid in live_ids:
            keep.add(k)
    return keep


def collect_garbage(fixtures):
    live_ids = {f["fixture"]["id"] for f in fixtures}
    for d in ("xg_previous", "xg_stats_cache"):
        for fid in list(state[d].keys()):
            if fid not in live_ids:
                del state[d][fid]
    state["mismatch_alerts_sent"] = {fid for fid in state["mismatch_alerts_sent"] if fid in live_ids}
    state["seen_red_cards"]       = _prune_prefixed_set(state["seen_red_cards"], live_ids)
    state["storm_alerts_sent"]    = _prune_prefixed_set(state["storm_alerts_sent"], live_ids)
    state["momentum_alerts_sent"] = _prune_prefixed_set(state["momentum_alerts_sent"], live_ids)


# ==========================================
# LOOPS
# ==========================================
async def fast_loop():
    channel = client.get_channel(CHANNEL_ROJAS_ID)
    print(f"[FAST] loop iniciado (cada {FAST_POLL}s) canal={CHANNEL_ROJAS_ID}")
    while not client.is_closed():
        try:
            if state["rojas_activo"]:
                fixtures = await fetch_fixtures_cached()
                is_startup = state["first_run_rojas"]
                alerts = detect_red_cards(fixtures, is_startup=is_startup)
                if is_startup:
                    state["first_run_rojas"] = False
                    if channel:
                        await channel.send(
                            f"🛡️ **Megabot iniciado (Tarjetas Rojas v5.2).**\n"
                            f"📡 Radar: `{len(fixtures)}` partidos en vivo."
                        )
                elif channel:
                    for a in alerts:
                        await channel.send(f"@everyone\n{a}")
                        await asyncio.sleep(1)
        except Exception as e:
            print(f"[FAST] error: {e}")
        await asyncio.sleep(FAST_POLL)


async def slow_loop():
    channel_xg = client.get_channel(CHANNEL_XG_ID)
    channel_mm = client.get_channel(CHANNEL_MISMATCH_ID)
    print(f"[SLOW] loop iniciado (cada {SLOW_POLL}s) xg={CHANNEL_XG_ID} mm={CHANNEL_MISMATCH_ID}")
    first_slow = True

    while not client.is_closed():
        try:
            fixtures = await fetch_fixtures_cached()
            important = [
                f for f in fixtures
                if es_partido_importante(
                    f["league"]["name"], f["league"]["country"],
                    f["teams"]["home"]["name"], f["teams"]["away"]["name"],
                )
            ]
            enriched = await enrich_with_xg(important)
            print(f"[SLOW] filtrados={len(important)} con_xg={len(enriched)}")

            if first_slow:
                for fixture, xg in enriched:
                    detect_momentum(fixture, xg, is_startup=True)
                first_slow = False
                if channel_xg:
                    await channel_xg.send(
                        f"📡 **Radar xG activo.** Analizando `{len(enriched)}` partidos "
                        f"filtrados cada `{SLOW_POLL}`s."
                    )
                if channel_mm:
                    await channel_mm.send(
                        f"⚖️ **Canal Desigualdades conectado.** "
                        f"Brecha mínima `{state['mismatch_threshold']}` goles netos."
                    )
            else:
                if state["xg_activo"] and channel_xg:
                    for fixture, xg in enriched:
                        msg = detect_momentum(fixture, xg, is_startup=False)
                        if msg:
                            await channel_xg.send(f"@everyone\n{msg}")
                            await asyncio.sleep(0.5)
                        for a in await detect_tormenta_perfecta(fixture, xg):
                            await channel_xg.send(a)
                            await asyncio.sleep(0.5)
                else:
                    for fixture, xg in enriched:
                        detect_momentum(fixture, xg, is_startup=True)

                if state["mismatch_activo"] and channel_mm:
                    for fixture, _xg in enriched:
                        msg = await detect_mismatch(fixture)
                        if msg:
                            await channel_mm.send(msg)
                            await asyncio.sleep(0.5)

            collect_garbage(fixtures)

        except Exception as e:
            print(f"[SLOW] error: {e}")
        await asyncio.sleep(SLOW_POLL)


# ==========================================
# COMANDOS DE DISCORD
# ==========================================
MODULES = {
    "rojas":    "rojas_activo",
    "xg":       "xg_activo",
    "mismatch": "mismatch_activo",
}


def _bulb(active):
    return "🟢" if active else "🔴"


def format_estado():
    return (
        f"🤖 **MEGABOT — PANEL DE CONTROL**\n"
        f"📡 Módulos activos:\n"
        f"  {_bulb(state['rojas_activo'])} Tarjetas Rojas  "
        f"{_bulb(state['xg_activo'])} Momentum/Tormenta  "
        f"{_bulb(state['mismatch_activo'])} Mismatch\n"
        f"📊 Cachés: `{len(state['xg_stats_cache'])}` partidos con xG | "
        f"`{len(state['team_stats_cache'])}` equipos guardados | "
        f"`{len(state['fixtures_cache'])}` fixtures\n"
        f"🎯 Umbral momentum: `+{state['momentum_threshold']}` xG | "
        f"Brecha mismatch: `{state['mismatch_threshold']}` goles\n"
        f"♻️ Fixtures cache: `{state['cache_hits_fixtures']}` hits / "
        f"`{state['cache_miss_fixtures']}` fetches"
    )


def format_partidos():
    if not state["xg_previous"]:
        return "💤 Radar vacío."
    lines = [f"🌍 **RADAR ACTIVO: {len(state['xg_previous'])} PARTIDOS**"]
    for fid, prev in state["xg_previous"].items():
        xg = state["xg_stats_cache"].get(fid)
        xg_txt = f"{xg['home_xg']}-{xg['away_xg']}" if xg else "—"
        lines.append(
            f"⏱️ `{prev['min']}'` | {prev['home']} **{prev['gh']}-{prev['ga']}** "
            f"{prev['away']} | xG: `{xg_txt}`"
        )
    return "\n".join(lines)[:1900]


def format_topxg():
    if not state["xg_stats_cache"]:
        return "💤 Sin datos de xG aún."
    rows = []
    for fid, xg in state["xg_stats_cache"].items():
        prev = state["xg_previous"].get(fid)
        if not prev:
            continue
        rows.append((xg["home_xg"] + xg["away_xg"], prev["min"], prev["home"], prev["away"]))
    if not rows:
        return "💤 Sin datos de xG aún."
    rows.sort(reverse=True)
    lines = ["🔥 **TOP 5 PARTIDOS MÁS CALIENTES (xG TOTAL):**"]
    for i, (xg_total, minute, home, away) in enumerate(rows[:5]):
        lines.append(f"**{i+1}.** ⏱️ `{minute}'` | {home} vs {away} | 🎯 **xG: {round(xg_total, 2)}**")
    return "\n".join(lines)


def format_buscar(query):
    q = query.strip().lower()
    if not q:
        return "❌ Uso: `!buscar <equipo>`"
    for fid, prev in state["xg_previous"].items():
        if q in prev["home"].lower() or q in prev["away"].lower():
            xg = state["xg_stats_cache"].get(fid)
            xg_txt = f"`{xg['home_xg']} - {xg['away_xg']}`" if xg else "`sin datos`"
            return (
                f"✅ **¡OBJETIVO EN EL RADAR!**\n"
                f"⏱️ Min `{prev['min']}'` | {prev['home']} **{prev['gh']}-{prev['ga']}** {prev['away']}\n"
                f"📊 **xG Actual:** {xg_txt}"
            )
    return f"❌ No se encontró `{query}` en el radar."


def format_ayuda():
    return (
        "🛠️ **MEGABOT — MENÚ DE COMANDOS**\n"
        "**Control de módulos:**\n"
        "  `!activar rojas` / `!desactivar rojas`\n"
        "  `!activar xg` / `!desactivar xg`\n"
        "  `!activar mismatch` / `!desactivar mismatch`\n"
        "**Consultas:**\n"
        "  `!estado` — panel de control\n"
        "  `!partidos` — partidos en radar con xG\n"
        "  `!topxg` — top 5 por xG total\n"
        "  `!buscar [equipo]` — busca equipo en el radar\n"
        "  `!setumbral [n]` — umbral momentum (default 0.15)\n"
        "  `!setbrecha [n]` — brecha mismatch (default 1.5)"
    )


@client.event
async def on_message(message):
    if message.author == client.user:
        return
    text  = message.content.strip()
    lower = text.lower()

    if lower in ("!comandos", "!help", "!ayuda"):
        await message.channel.send(format_ayuda())

    elif lower == "!estado":
        await message.channel.send(format_estado())

    elif lower == "!partidos":
        await message.channel.send(format_partidos())

    elif lower == "!topxg":
        await message.channel.send(format_topxg())

    elif lower.startswith("!buscar "):
        await message.channel.send(format_buscar(text[len("!buscar "):]))

    elif lower.startswith("!setumbral "):
        try:
            v = float(text.split(None, 1)[1])
            state["momentum_threshold"] = v
            await message.channel.send(f"✅ Umbral de Momentum actualizado a `+{v}`.")
        except (ValueError, IndexError):
            await message.channel.send("❌ Formato: `!setumbral 0.25`")

    elif lower.startswith("!setbrecha "):
        try:
            v = float(text.split(None, 1)[1])
            state["mismatch_threshold"] = v
            await message.channel.send(f"✅ Brecha de mismatch actualizada a `{v}`.")
        except (ValueError, IndexError):
            await message.channel.send("❌ Formato: `!setbrecha 2.0`")

    elif lower.startswith("!activar ") or lower.startswith("!desactivar "):
        parts = lower.split(None, 1)
        if len(parts) < 2 or parts[1] not in MODULES:
            await message.channel.send(f"❌ Módulos válidos: {', '.join(MODULES)}")
            return
        key    = MODULES[parts[1]]
        activo = parts[0] == "!activar"
        state[key] = activo
        estado_txt = "activado" if activo else "desactivado"
        await message.channel.send(f"✅ Módulo `{parts[1]}` {estado_txt}.")


@client.event
async def on_ready():
    print(f"✅ Megabot conectado como {client.user}")
    client.loop.create_task(fast_loop())
    client.loop.create_task(slow_loop())


if __name__ == "__main__":
    if not DISCORD_TOKEN:
        print("❌ ERROR: Falta la variable de entorno DISCORD_TOKEN.")
    elif not API_FOOTBALL_KEY:
        print("❌ ERROR: Falta la variable de entorno API_FOOTBALL_KEY.")
    else:
        client.run(DISCORD_TOKEN)
