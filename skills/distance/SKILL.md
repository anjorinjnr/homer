---
name: distance
description: "Get travel distance and time between two locations using Google Maps Distance Matrix. Supports driving, walking, bicycling, and transit. Default origin is the household home (see `context/household.md`)."
metadata: {"nanobot":{"emoji":"🚗"}}
---

# Distance Skill

Calculate travel time and distance between two locations.

## Quick Start

```
exec python tools/maps.py --mode distance --destination "Atlanta Airport"
```

Default origin is the **household home** (read from `context/household.md`). Specify `--origin` to override.

**Examples:**
```
# From home to a destination (driving)
exec python tools/maps.py --mode distance --destination "Hartsfield-Jackson Atlanta Airport"

# Custom origin
exec python tools/maps.py --mode distance --origin "123 Main St, Othertown ST" --destination "Atlanta, GA"

# Different travel mode
exec python tools/maps.py --mode distance --destination "Othertown, ST" --travel-mode walking

# Transit
exec python tools/maps.py --mode distance --origin "Atlanta, GA" --destination "Buckhead, Atlanta GA" --travel-mode transit
```

## Parameters

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--destination` | string | Required | Destination address or place name |
| `--origin` | string | household home | Origin address (omit to use household home) |
| `--travel-mode` | string | `driving` | `driving`, `walking`, `bicycling`, `transit` |

## Response Format

```json
{
  "origin": "100 Home St, Anytown, ST 12345, USA",
  "destination": "Hartsfield-Jackson Atlanta International Airport, Atlanta, GA 30320, USA",
  "distance": "28.4 mi",
  "duration": "38 mins",
  "travel_mode": "driving"
}
```

## Examples

### Drive to Airport
```
exec python tools/maps.py --mode distance --destination "Hartsfield-Jackson Atlanta Airport"
```

### How Far is a Business Found via Places
```
exec python tools/maps.py --mode distance --destination "100 Main St, Anytown ST"
```

### Compare Driving vs Transit
```
exec python tools/maps.py --mode distance --destination "Downtown Atlanta" --travel-mode driving
exec python tools/maps.py --mode distance --destination "Downtown Atlanta" --travel-mode transit
```

## Tips

- **Omit `--origin`** when the user says "from home", "from here", or doesn't specify — the household home is the default
- **Use full address or well-known name** for best accuracy (e.g. "Hartsfield-Jackson Atlanta Airport" not just "airport")
- **`duration` reflects typical traffic** for driving — it does not account for real-time conditions
- **Combine with places** — after finding a business with the places skill, pass its address to distance to tell the user how far it is
