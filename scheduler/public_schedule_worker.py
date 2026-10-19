"""Read a public GameSheet schedule with an explicitly selected local runtime.

This standalone process accepts only a date and season ID, never CRM secrets or
arbitrary URLs. It deliberately has no Django/project imports.
"""
import json
import socket
import sys
from datetime import date

import requests


def main():
    task = json.load(sys.stdin)
    day = date.fromisoformat(task["date"]).isoformat()
    season = int(task["season"])
    if not 0 < season < 10000000:
        raise ValueError("Invalid public season")
    original = socket.getaddrinfo

    def ipv4(host, port, family=0, type=0, proto=0, flags=0):
        return original(host, port, socket.AF_INET, type, proto, flags)

    socket.getaddrinfo = ipv4
    response = requests.get(
        f"https://gamesheetstats.com/api/unified-games/{season}",
        params={"dateStart": day, "dateEnd": day},
        timeout=20,
    )
    json.dump({
        "status_code": response.status_code,
        "payload": response.json() if response.status_code == 200 else None,
    }, sys.stdout)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Do not print request headers, environment, or exception URLs.
        print(json.dumps({"error": type(exc).__name__}))
        sys.exit(1)
