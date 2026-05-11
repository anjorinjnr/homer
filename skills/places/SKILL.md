---
name: places
description: "Search for local businesses and places using Google Maps. Returns name, address, rating, hours, phone, and website. Use when the user wants to find a nearby business, service, or venue."
metadata: {"nanobot":{"emoji":"📍"}}
---

# Places Skill

Search for local businesses and get detailed information including hours, phone number, ratings, and website.

## Quick Start

```
exec python tools/maps.py --mode places --query "Italian restaurants near Anytown USA"
```

**Examples:**
```
# Search with location in query
exec python tools/maps.py --mode places --query "plumbers near Anytown USA"

# Search with separate --near flag
exec python tools/maps.py --mode places --query "urgent care" --near "Anytown, USA"

# Limit results
exec python tools/maps.py --mode places --query "coffee shops near Anytown USA" --max-results 3
```

## Getting Full Details

The `places` mode returns summary results with `place_id`. Use `details` mode to get hours, phone number, and website for a specific place:

```
exec python tools/maps.py --mode details --place-id "ChIJxxxxxxxx"
```

**Typical flow for "find me a good plumber":**
1. Run `places` to get top results with ratings
2. Run `details` on the top-rated result to get phone number and hours
3. Present the full info to the user

## Parameters

### places mode

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--query` | string | Required | Search query (business type + location, or just type with `--near`) |
| `--near` | string | — | Location to search near (appended to query) |
| `--max-results` | integer | 5 | Number of results to return (1-20) |

### details mode

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--place-id` | string | Required | Google Place ID from a `places` search result |

## Response Format

### places
```json
{
  "query": "Italian restaurants near Anytown USA",
  "results": [
    {
      "name": "Bravo Italian Kitchen",
      "address": "100 Main St, Anytown, ST 12345",
      "rating": 4.3,
      "user_ratings_total": 842,
      "open_now": true,
      "place_id": "ChIJxxxxxxxx",
      "types": ["restaurant", "food", "establishment"]
    }
  ]
}
```

### details
```json
{
  "name": "Bravo Italian Kitchen",
  "address": "100 Main St, Anytown, ST 12345",
  "phone": "(770) 555-1234",
  "website": "https://example.com",
  "maps_url": "https://maps.google.com/?cid=...",
  "rating": 4.3,
  "user_ratings_total": 842,
  "open_now": true,
  "hours": [
    "Monday: 11:00 AM – 10:00 PM",
    "Tuesday: 11:00 AM – 10:00 PM",
    ...
  ]
}
```

## Examples

### Find a Service Provider
```
exec python tools/maps.py --mode places --query "HVAC repair near Anytown USA" --max-results 3
```
Then for the top result:
```
exec python tools/maps.py --mode details --place-id "<place_id from above>"
```

### Find a Restaurant
```
exec python tools/maps.py --mode places --query "sushi restaurants near Anytown USA"
```

### Check Business Hours
```
exec python tools/maps.py --mode details --place-id "<place_id>"
```

## Tips

- **Include location in query** or use `--near` — Google Maps needs a location anchor for relevant results
- **Default household location lives in `context/household.md`** — use it when the user says "near me" or "nearby"
- **Chain places → details** when the user needs hours, phone, or website — `places` mode returns `place_id` for this
- **Rating + user_ratings_total** together indicate reliability — a 4.5 with 12 ratings is less reliable than a 4.2 with 800
- **open_now** in `places` results may be absent if hours aren't set — confirm with `details` if it matters
