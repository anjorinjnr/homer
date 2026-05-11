#!/usr/bin/env python3
"""
parse_vcard.py — Extract name and phone number from vCard (VCF) data.

This tool handles contact attachments shared via WhatsApp. It uses regex
to parse the VCARD format and return a structured JSON result.

Usage:
    python tools/parse_vcard.py --vcard "BEGIN:VCARD\nVERSION:3.0\nFN:Jake\nTEL;type=CELL:+15551234567\nEND:VCARD"
    python tools/parse_vcard.py --file /path/to/contact.vcf
"""

import argparse
import json
import re
import sys


def parse_vcard(content: str) -> dict:
    """Parse vCard content and return {name, phone}."""
    if "BEGIN:VCARD" not in content.upper():
        return {"error": "No vCard data found in input"}

    # Extract Full Name (FN)
    # Supports: FN:Jake or FN;CHARSET=UTF-8:Jake
    name_match = re.search(r"^FN(?:;[^:]*)?:(.+)$", content, re.MULTILINE | re.IGNORECASE)
    name = name_match.group(1).strip() if name_match else "Unknown"

    # Extract Phone Number (TEL)
    # Supports: TEL:+1555... or TEL;type=CELL:+1555... or TEL;VALUE=uri:tel:+1555...
    phones = re.findall(r"^TEL(?:;[^:]*)?:(?:tel:)?(.+)$", content, re.MULTILINE | re.IGNORECASE)

    phone = ""
    for p in phones:
        # Clean up the phone number (remove spaces, parens, dashes)
        clean_p = re.sub(r"[^\d+]", "", p.strip())
        if clean_p:
            phone = clean_p
            break

    if not phone:
        return {"error": "No phone number found in vCard"}

    return {"name": name, "phone": phone}


def main() -> None:
    parser = argparse.ArgumentParser(description="Parse vCard data and extract name and phone.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--vcard", help="Raw vCard string")
    group.add_argument("--file", help="Path to a .vcf file")
    args = parser.parse_args()

    try:
        if args.vcard:
            content = args.vcard
        else:
            with open(args.file, "r", encoding="utf-8") as f:
                content = f.read()

        result = parse_vcard(content)
        if "error" in result:
            print(json.dumps(result))
            sys.exit(1)
        print(json.dumps(result))
    except FileNotFoundError as e:
        print(json.dumps({"error": f"File not found: {e}"}))
        sys.exit(1)
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)


if __name__ == "__main__":
    main()
