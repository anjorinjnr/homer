---
name: weather
description: Get current weather and forecasts. Default location is the household home (see `context/household.md`).
metadata: {"nanobot":{"emoji":"🌤️","requires":{"bins":["curl"]}}}
---

# Weather

Default location: the **household home** (see `context/household.md`). Use a user-provided location if specified.

## Primary: Open-Meteo (free, no API key)

**Step 1 — Geocode the location:**
```bash
curl -s "https://geocoding-api.open-meteo.com/v1/search?name=<city>+<state>&count=1&language=en&format=json"
```
Extract `latitude` and `longitude` from the first result. If you already have the home coordinates from context, you can skip this step.

**Step 2 — Fetch current weather + 3-day forecast:**
```bash
curl -s "https://api.open-meteo.com/v1/forecast?latitude=<lat>&longitude=<lon>&current=temperature_2m,apparent_temperature,precipitation,weathercode,windspeed_10m,relativehumidity_2m&daily=temperature_2m_max,temperature_2m_min,precipitation_sum,precipitation_probability_max,weathercode&temperature_unit=fahrenheit&windspeed_unit=mph&precipitation_unit=inch&timezone=<tz>&forecast_days=3"
```

Weather code → condition:
- 0: Clear sky · 1-3: Partly/mostly cloudy · 45,48: Fog
- 51-67: Drizzle/rain · 71-77: Snow · 80-82: Rain showers
- 85-86: Snow showers · 95: Thunderstorm · 96,99: Severe thunderstorm

## Fallback: NWS / weather.gov (US only)

If Open-Meteo fails or returns an error, use NWS:

**Step 1 — Get grid metadata:**
```bash
curl -s "https://api.weather.gov/points/<lat>,<lon>"
```
Extract the `forecast` and `forecastHourly` URLs from the `properties` object.

**Step 2 — Get forecast:**
```bash
curl -s "<forecast_url_from_step1>"
```
Use the first period for current conditions and today's high/low.

## Response format

Write in a natural, conversational tone — not a data dump. Format:

1. **Header**: "Today in [City, ST]:"
2. **Current + today**: emoji, plain-English conditions, high °F, notable wind if strong. Add a brief
   human note if warranted ("nice day!", "stay dry").
3. **Tonight**: emoji, conditions, low °F.
4. **Heads up** (only if notable): flag significant upcoming weather in the next 1–2 days
   (storms, big temp swings, high rain probability). Skip if nothing unusual.

Use emojis naturally (⛅ 🌧️ ⛈️ ☀️ 🌙 ❄️). Skip humidity/feels-like/wind unless they're
the story. No raw numbers lists, no JSON, no curl commands.
