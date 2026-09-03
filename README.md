# Megabot

Bot único de Discord que unifica las estrategias de `bot-rojas`, `bot-sniper-xg` y `mismatch-snipe`, compartiendo la caché de fixtures y aplicando el filtro de ligas v5.2 a todas las estrategias.

## Estrategias

| Estrategia | Loop | Canal |
|---|---|---|
| Tarjetas rojas (directa + doble amarilla) | fast (20s) | `CHANNEL_ROJAS_ID` |
| Momentum xG (salto entre ciclos) | slow (120s) | `CHANNEL_XG_ID` |
| Tormenta Perfecta (0 goles + xG alto + historial fuerte) | slow (120s) | `CHANNEL_XG_ID` |
| Caza-Desigualdades (brecha histórica entre equipos) | slow (120s) | `CHANNEL_MISMATCH_ID` |

## Ahorro de API calls

| | calls/día |
|---|---|
| 3 bots por separado | ~53.280 |
| Megabot unificado | ~15.180 |
| **Ahorro** | **~71%** |

Desglose megabot:
- Fixtures cada 20s: 4.320
- xG (~15 partidos filtrados cada 120s): ~10.800
- Team stats (caché 24h, ~60 equipos): ~60

## Variables de entorno (Railway)

```
DISCORD_TOKEN         Token del bot de Discord
API_FOOTBALL_KEY      Key de api-sports.io
CHANNEL_ROJAS_ID      ID canal para tarjetas rojas
CHANNEL_XG_ID         ID canal para Momentum xG + Tormenta Perfecta
CHANNEL_MISMATCH_ID   ID canal para Caza-Desigualdades
```

Para copiar el ID de un canal: Discord → Ajustes → Avanzado → Modo desarrollador ✓, luego click derecho sobre el canal → *Copiar ID*.

## Comandos

**Control de módulos**
- `!activar rojas` / `!desactivar rojas`
- `!activar xg` / `!desactivar xg`
- `!activar mismatch` / `!desactivar mismatch`

**Consultas**
- `!estado` — panel de control (módulos activos, cachés, umbrales, hits)
- `!partidos` — lista partidos en radar con xG
- `!topxg` — top 5 partidos por xG total
- `!buscar [equipo]` — busca equipo en el radar
- `!setumbral [n]` — cambia el umbral de Momentum xG (default `0.15`)
- `!setbrecha [n]` — cambia el umbral de Mismatch (default `1.5`)
- `!comandos` / `!help` — menú de comandos

## Deploy

Railway auto-detecta Python vía `requirements.txt`; el `Procfile` declara el proceso como `worker`.
