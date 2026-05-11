---
name: vault
description: Securely store and retrieve sensitive reference data like loyalty numbers, recovery codes, and account numbers.
metadata: {"nanobot":{"always":false,"emoji":"🔐"}}
---

# Vault — Secure Reference Data

Store and retrieve sensitive reference numbers: loyalty programs, recovery codes, account numbers, PINs, and similar data that should not sit in memory or context files.

## When to use

- User says "remember my [loyalty/account/membership] number"
- User asks "what's my [Marriott/Bonvoy/Plaid/etc.] number?"
- Any task that requires a stored reference number (booking, account lookup, etc.)
- User says "store this securely" or "save this to the vault"

## Important rules

1. **Never store in MEMORY.md.** Loyalty numbers, recovery codes, account numbers, and similar sensitive values must go in the vault — never in nanobot memory or context files.
2. **Never echo values back verbatim.** When you retrieve a value, use it to complete the task (e.g., include it in a booking message) but do not repeat it back as "your number is X" unless the user specifically asked to see it.
3. **Use `--label`** when storing so `--list` output is meaningful.
4. **Use lowercase_snake_case keys** for consistency (e.g., `marriott_bonvoy`, `plaid_recovery_code`).

## Tool reference

### Store a value
```
{HOMER_VENV} {HOMER_TOOLS}/vault.py --set "<key>" "<value>" --label "<description>"
```

### Retrieve a value
```
{HOMER_VENV} {HOMER_TOOLS}/vault.py --get "<key>"
```

### List all keys (shows labels, not values)
```
{HOMER_VENV} {HOMER_TOOLS}/vault.py --list
```

### Remove a value
```
{HOMER_VENV} {HOMER_TOOLS}/vault.py --remove "<key>"
```

## Examples

Store a loyalty number:
```
{HOMER_VENV} {HOMER_TOOLS}/vault.py --set "marriott_bonvoy" "072663520" --label "Marriott Bonvoy loyalty number"
```

Retrieve for a booking task:
```
{HOMER_VENV} {HOMER_TOOLS}/vault.py --get "marriott_bonvoy"
```
→ Use the returned value in the booking flow. Do not repeat it back to the user.
