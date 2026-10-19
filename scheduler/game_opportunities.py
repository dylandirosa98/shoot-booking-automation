"""
Day-by-day Game Opportunity Research across the 5 target hockey schedule sites:
1. AYHL (https://atlantichockey.org/ayhl.php -> full_schedule.php)
2. GameSheet (https://www.gamesheetinc.com/ -> gamesheetstats.com/api/unified-games)
3. AHF (https://atlantichockeyfederation.com/ -> api.blackbear.timetoscore.com)
4. THF (https://www.tier1hockeyfederation.com/ -> api.blackbear.timetoscore.com)
5. FedHockey / EHF (https://fedhockey.com/ -> EHF GameSheet seasons 15008 / 14728)

Runs locally on the operator's machine, matches public games at our shoot rinks
against indexed Pipedrive records (club/age candidates + maybes), saves to
ignored `var/game_opportunities.json` + `GameOpportunitySnapshot`, and can push
results directly to the Railway deployment.
"""
from __future__ import annotations

import codecs
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import re
import signal
import shutil
import socket
import subprocess
import tempfile
import time
from html.parser import HTMLParser
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from dotenv import dotenv_values
import requests


class _HTMLStripper(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in {"br", "p", "div", "li", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)

    def get_text(self, sep: str = "\n") -> str:
        raw = "".join(self.parts)
        lines = [line.strip() for line in raw.splitlines() if line.strip()]
        return sep.join(lines)


def _html_to_text(raw_html: str, sep: str = "\n") -> str:
    if not raw_html:
        return ""
    stripper = _HTMLStripper()
    stripper.feed(raw_html)
    return stripper.get_text(sep=sep)


def _redact_pii(text: str) -> str:
    if not text:
        return ""
    s = re.sub(r"[\w.+-]+@[\w-]+\.[\w.-]+", "[email]", text)
    s = re.sub(r"(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}\b", "[phone]", s)
    return s

logger = logging.getLogger(__name__)


class ScheduleResearchError(RuntimeError):
    """A source failed; do not save or publish this incomplete refresh."""


DATA_FILE = Path(__file__).resolve().parent.parent / "var" / "game_opportunities.json"

TARGET_SITES = [
    {"name": "AYHL", "url": "https://atlantichockey.org/ayhl.php"},
    {"name": "GameSheet", "url": "https://www.gamesheetinc.com/"},
    {"name": "AHF", "url": "https://atlantichockeyfederation.com/"},
    {"name": "THF", "url": "https://www.tier1hockeyfederation.com/"},
    {"name": "FedHockey (EHF)", "url": "https://fedhockey.com/"},
]

CREDENTIAL_ENV_PATHS = [
    Path(__file__).resolve().parent.parent / ".env",
    Path("/home/dylandirosa/PuckProMedia/puckpro-sales-automation/.env"),
]

USER_AGENT = {"User-Agent": "Mozilla/5.0 (compatible; PuckProResearch/1.0)"}

# Force IPv4 resolution when outbound IPv6 is unavailable on local Linux hosts
_ORIG_GETADDRINFO = socket.getaddrinfo


def _enable_ipv4_fastpath():
    def _ipv4_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        return _ORIG_GETADDRINFO(host, port, socket.AF_INET, type, proto, flags)

    socket.getaddrinfo = _ipv4_getaddrinfo


# ---------------------------------------------------------------------------
# Rink Canonicalization & Region Mapping
# ---------------------------------------------------------------------------

RINK_REGION = {
    "New England Sports Center": "NE",
    "Pilgrim Skating Arena": "NE",
    "Hallenborg Ice Rink": "NE",
    "Foxboro Sports Center": "NE",
    "Rhode Island Sports Center (Adelard Arena)": "NE",
    "Fidelity Bank Worcester Ice Center": "NE",
    "Boch Ice Center": "NE",
    "Canton Ice House": "NE",
    "Lovell Arena": "NE",
    "Breakaway Ice Center": "NE",
    "Skate 3": "NE",
    "The Rinks at Exeter": "NE",
    "New England Sports Village": "NE",
    "Northstar Ice Sports": "NE",
    "Thayer Sports Center": "NE",
    "Essex Sports Center": "NE",
    "Icenter Salem": "NE",
    "Rockland Skating Rink": "NE",
    "Hobomock Arena": "NE",
    "Great American Center (roller hockey)": "ROLLER",
    "Brewster Ice Arena": "NY",
    "Northwell Health Ice Center": "NY",
    "Rochester Ice Center (Fairport)": "NY",
    "Utica / Nexus Center": "NY",
    "Ice Time Sports Complex (Newburgh)": "NY",
    "Sport-O-Rama": "NY",
    "Iceworks Syosset": "NY",
    "Chelsea Piers Sky Rink": "NY",
    "Chelsea Piers CT": "NE",
    "Stamford Twin Rinks": "NE",
    "SoNo Ice House": "NE",
    "Danbury Ice Arena": "NE",
    "Red Bank Armory": "MID",
    "Ice Land Skating Center": "MID",
    "Bridgewater Sports Arena (NJ)": "MID",
    "Pennsauken Skate Zone": "MID",
    "Ice Vault Arena": "MID",
    "Grundy Ice Arena": "MID",
    "Woodbridge Community Center": "MID",
    "Igloo Ice Rink": "MID",
    "Hollydell Ice Arena": "MID",
    "Ice House (Hackensack)": "MID",
    "Oaks Center Ice": "MID",
    "Klick Lewis Arena": "MID",
    "IceWorks Skating Complex (Aston)": "MID",
    "Hatfield Ice Arena": "MID",
    "Pegula Ice Arena": "MID",
    "Ice Line": "MID",
    "Middletown Sports Complex": "MID",
    "Flyers Training Center": "MID",
    "Mennen Sports Arena": "MID",
    "Twin Oaks Ice Rink": "MID",
    "Pro Skate": "MID",
    "Rock Ice Center (Dunellen)": "MID",
    "Winding River Skating Center": "MID",
    "Jersey Shore Arena": "MID",
    "Marple Sports Arena": "MID",
    "Revolution Ice Gardens": "MID",
    "Northeast Skate Zone": "MID",
    "Bucks County Ice Sports Center": "MID",
    "Body Zone Sports and Wellness Complex": "MID",
}


def canonical_rink(raw_loc: str, league_context: str = "") -> str | None:
    """Map Pipedrive shoot addresses and league venue strings to a canonical facility."""
    s = " ".join((raw_loc or "").lower().split())
    ctx = (league_context or "").lower()
    if not s:
        return None
    if "essex sports center" in s or "essex sports complex" in s:
        return "Essex Sports Center"
    if ("icenter" in s or "i-center" in s or "i center" in s) and "salem" in s:
        return "Icenter Salem"
    if "rockland skating" in s or "rockland ice rink" in s:
        return "Rockland Skating Rink"
    # Northstar operates Hobomock too; it is not the Westborough facility.
    if "hobomock" in s:
        return "Hobomock Arena"
    if "great american center" in s:
        return "Great American Center (roller hockey)"
    if "new england sports center" in s or re.search(r"\bnesc\b", s):
        return "New England Sports Center"
    if "red bank armory" in s:
        return "Red Bank Armory"
    if (
        "ice land black bear" in s
        or "ice land phantoms" in s
        or ("ice land" in s and "hamilton" in s)
        or ("ice land" in s and "long island" not in ctx)
    ):
        return "Ice Land Skating Center"
    if "pilgrim" in s:
        return "Pilgrim Skating Arena"
    if (
        "bridgewater sports arena" in s
        or "rsg arenas at bridgewater" in s
        or ("bridgewater" in s and ("nj" in s or "frontier" in s or "rsg" in s or "njyhl" in ctx))
    ):
        return "Bridgewater Sports Arena (NJ)"
    if "pennsauken" in s:
        return "Pennsauken Skate Zone"
    if "brewster" in s:
        return "Brewster Ice Arena"
    if "northwell" in s or ("eisenhower" in s and "twin rinks" in s):
        return "Northwell Health Ice Center"
    if "ice vault" in s:
        return "Ice Vault Arena"
    if "grundy" in s:
        return "Grundy Ice Arena"
    if (
        "woodbridge community" in s
        or "wcc ice rink" in s
        or "woodbridge comminuty" in s
        or "club at woodbridge" in s
    ):
        return "Woodbridge Community Center"
    if (
        "fairport" in s
        or "rochester ice center" in s
        or "rocheser ice center" in s
        or re.search(r"\bric\s*\d", s)
    ):
        return "Rochester Ice Center (Fairport)"
    if "igloo" in s:
        return "Igloo Ice Rink"
    if "hallenborg" in s:
        return "Hallenborg Ice Rink"
    if "foxboro" in s:
        return "Foxboro Sports Center"
    if "hollydell" in s:
        return "Hollydell Ice Arena"
    if "ice house" in s and not any(
        x in s for x in ["canton", "sono", "darien", "ashburn", "gardens", "hockey house"]
    ):
        return "Ice House (Hackensack)"
    if "rhode island sports center" in s or "adelard" in s:
        return "Rhode Island Sports Center (Adelard Arena)"
    if "utica" in s or "nexus center" in s or "adirondack bank center" in s:
        return "Utica / Nexus Center"
    if "worcester ice center" in s or "fidelity bank worcester" in s:
        return "Fidelity Bank Worcester Ice Center"
    if "oaks center ice" in s:
        return "Oaks Center Ice"
    if "boch ice" in s:
        return "Boch Ice Center"
    if "canton ice house" in s or "canton sportsplex" in s:
        return "Canton Ice House"
    if "klick lewis" in s:
        return "Klick Lewis Arena"
    if s == "aston" or (
        ("iceworks" in s or "ice works" in s)
        and "syosset" not in s
        and "long island" not in ctx
    ):
        return "IceWorks Skating Complex (Aston)"
    if "iceworks" in s or "ice works" in s:
        return "Iceworks Syosset"
    if "hatfield" in s:
        return "Hatfield Ice Arena"
    if "pegula" in s:
        return "Pegula Ice Arena"
    if "ice line" in s:
        return "Ice Line"
    if "middletown sports" in s or "middletown ice" in s:
        return "Middletown Sports Complex"
    if "flyers training center" in s:
        return "Flyers Training Center"
    if "mennen" in s:
        return "Mennen Sports Arena"
    if "twin oaks" in s:
        return "Twin Oaks Ice Rink"
    if "pro skate" in s:
        return "Pro Skate"
    if "dunellen" in s or "rock ice center" in s:
        return "Rock Ice Center (Dunellen)"
    if "winding river" in s:
        return "Winding River Skating Center"
    if "jersey shore arena" in s:
        return "Jersey Shore Arena"
    if "marple sports" in s:
        return "Marple Sports Arena"
    if "revolution ice gardens" in s:
        return "Revolution Ice Gardens"
    if "northeast skate zone" in s:
        return "Northeast Skate Zone"
    if "bucks county ice" in s:
        return "Bucks County Ice Sports Center"
    if "body zone" in s:
        return "Body Zone Sports and Wellness Complex"
    if "lovell" in s:
        return "Lovell Arena"
    if "breakaway ice" in s:
        return "Breakaway Ice Center"
    if "skate three" in s or "skate 3" in s:
        return "Skate 3"
    if "rinks at exeter" in s:
        return "The Rinks at Exeter"
    if "new england sports village" in s:
        return "New England Sports Village"
    if "northstar ice" in s:
        return "Northstar Ice Sports"
    if "thayer sports" in s:
        return "Thayer Sports Center"
    if "sono ice house" in s:
        return "SoNo Ice House"
    if "stamford twin rinks" in s:
        return "Stamford Twin Rinks"
    if "chelsea piers connecticut" in s or "chelsea piers ct" in s:
        return "Chelsea Piers CT"
    if "chelsea piers sky rink" in s:
        return "Chelsea Piers Sky Rink"
    if "danbury ice" in s:
        return "Danbury Ice Arena"
    if "sport-o-rama" in s:
        return "Sport-O-Rama"
    if "ice time sports" in s:
        return "Ice Time Sports Complex (Newburgh)"
    return None


# ---------------------------------------------------------------------------
# Pipedrive Client & Full-CRM Index
# ---------------------------------------------------------------------------

def get_pipedrive_credentials() -> tuple[str, str]:
    token = os.environ.get("PIPEDRIVE_API_TOKEN", "").strip()
    domain = os.environ.get("PIPEDRIVE_COMPANY_DOMAIN", "").strip()
    if token and domain:
        return token, domain.removesuffix(".pipedrive.com")
    for path in CREDENTIAL_ENV_PATHS:
        if path.exists():
            vals = dotenv_values(path)
            t = (vals.get("PIPEDRIVE_API_TOKEN") or "").strip()
            d = (vals.get("PIPEDRIVE_COMPANY_DOMAIN") or "").strip()
            if t and d:
                return t, d.removesuffix(".pipedrive.com")
    raise RuntimeError("PIPEDRIVE_API_TOKEN and PIPEDRIVE_COMPANY_DOMAIN are required.")


def pipedrive_get_all(token: str, domain: str, resource: str, **filters) -> list[dict]:
    rows: list[dict] = []
    start = 0
    cursor = None
    for _ in range(100):
        params = {"api_token": token, "limit": 500, **filters}
        if cursor:
            params["cursor"] = cursor
        else:
            params["start"] = start
        resp = requests.get(
            f"https://{domain}.pipedrive.com/api/v1/{resource}",
            params=params,
            timeout=30,
        )
        resp.raise_for_status()
        payload = resp.json()
        data = payload.get("data") or []
        rows.extend(data)
        add = payload.get("additional_data") or {}
        pag = add.get("pagination") or {}
        next_cursor = add.get("next_cursor") or pag.get("next_cursor")
        if next_cursor:
            cursor = next_cursor
        elif pag.get("more_items_in_collection"):
            start = pag["next_start"]
        else:
            break
    return rows


# ---------------------------------------------------------------------------
# Age Group / Birth Year & Club Matching
# ---------------------------------------------------------------------------

# In the 2026-2027 USA Hockey season:
YEAR_TO_AGES = {
    "2018": {"8u"},
    "2017": {"9u", "10u"},
    "2016": {"10u"},
    "2015": {"11u", "12u"},
    "2014": {"12u"},
    "2013": {"13u", "14u"},
    "2012": {"14u"},
    "2011": {"15u", "16u"},
    "2010": {"16u"},
    "2009": {"18u"},
    "2008": {"18u"},
}


def extract_ages(text: str) -> set[str]:
    s = (text or "").lower()
    ages: set[str] = set()
    # Match patterns like U16, 16U, 14UAA, 14AA, 12AAA, 12M, 15O, 13 Pure, 12 years old
    for m in re.finditer(
        r"\b(?:u\s*(\d{1,2})|(\d{1,2})\s*(?:u(?:aaa|aa|a|int|beg|adv)?|o|aaa|aa|gry|l|m|int|pure|years?\s+old|yo))\b",
        s,
    ):
        num = m.group(1) or m.group(2)
        if num in {"8", "9", "10", "11", "12", "13", "14", "15", "16", "18"}:
            ages.add(f"{num}u")
    for yr, mapped in YEAR_TO_AGES.items():
        if re.search(rf"\b{yr}\b", s):
            ages.update(mapped)
    for word, mapped in [
        ("mite", "8u"),
        ("squirt", "10u"),
        ("peewee", "12u"),
        ("pee wee", "12u"),
        ("bantam", "14u"),
        ("midget minor", "16u"),
        ("midget major", "18u"),
    ]:
        if re.search(rf"\b{word}\b", s):
            ages.add(mapped)
    return ages


def _ages_within_one_year(ages_a: set[str], ages_b: set[str]) -> bool:
    nums_a = {int(a[:-1]) for a in ages_a if a.endswith("u") and a[:-1].isdigit()}
    nums_b = {int(b[:-1]) for b in ages_b if b.endswith("u") and b[:-1].isdigit()}
    for na in nums_a:
        for nb in nums_b:
            if abs(na - nb) <= 1:
                return True
    return False


# High-precision club regexes covering common abbreviations/aliases across the 5 sites & CRM notes
CLUB_RULES: list[tuple[str, str]] = [
    ("Minuteman Flames", r"\bminutem[ae]n\s+flames\b"),
    ("Minuteman Sparks", r"\bminutem[ae]n\s+sparks\b"),
    ("Boston Jr. Eagles", r"\b(?:boston\s+)?j(?:unio)?r\.?\s+eagles\b"),
    ("Boston Jr. Terriers", r"\b(?:boston\s+)?j(?:unio)?r\.?\s+terriers\b"),
    ("Boston Jr. Huskies", r"\b(?:boston\s+)?j(?:unio)?r\.?\s+huskies\b"),
    ("Boston Jr. Dogs", r"\b(?:boston\s+)?j(?:unio)?r\.?\s+dogs\b"),
    ("Boston Jr. Bruins", r"\b(?:gbl\s+)?(?:boston\s+)?j(?:unio)?r\.?\s+bruins\b"),
    ("Little Bruins", r"\blittle\s+bruins\b"),
    ("Boston Vipers", r"\bboston\s+vipers\b"),
    ("Boston Advantage", r"\bboston\s+advantage\b"),
    ("Bay State Breakers", r"\bbay\s*state\s+breakers\b"),
    ("South Shore Kings", r"\bsouth\s+shore\s+kings\b|\bss\s+kings\b"),
    ("Cape Cod Whalers", r"\bcape\s+cod\s+whalers\b"),
    ("Central Mass Penguins", r"\bcentral\s+mass\s+penguins\b|\bcm\s+penguins\b"),
    ("Top Gun", r"\btop\s*gun\b"),
    ("Islanders Hockey Club", r"\bislanders\s+hockey\s+club\b|\bihc\b"),
    ("Northern Cyclones", r"\bnorthern\s+cyclones\b|\bcyclones\s+hockey\b"),
    ("Northeast Generals", r"\bnortheast\s+generals\b"),
    ("American Hockey Academy", r"\bamerican\s+hockey\s+academy\b"),
    ("Boch Blazers", r"\bboch\s+blazers\b"),
    ("Providence Capitals", r"\bprovidence\s+cap(?:ital)?s\b"),
    ("Seacoast Spartans", r"\bseacoast\s+spartans\b"),
    ("Bulldogs Hockey Club", r"\bbulldogs\s+hockey\s+club\b"),
    ("North Suburban Wings", r"\b(?:ns|north\s+suburban)\s+wings\b"),
    ("Young Guns", r"\byoung\s+guns\b"),
    ("Team Merrimack", r"\bteam\s+merrimack\b"),
    ("NorthStars", r"\bnorth\s*stars\b"),
    ("Charles River", r"\bcharles\s+river\b"),
    ("Assabet Valley", r"\bassabet(?:\s+valley)?\b"),
    ("East Coast Wizards", r"\beast\s+coast\s+wizards\b"),
    ("Boston Lady Whalers", r"\b(?:boston\s+)?lady\s+whalers\b"),
    ("Mass Spitfires", r"\b(?:mass\s+)?spitfires\b"),
    ("NAHA", r"\bnaha\b"),
    ("Lovell Knights", r"\blovell\s+(?:knights|winter\s+club|hockey)\b"),
    ("NRI Vikings", r"\bnri\s+vikings\b"),
    ("KP Walpole", r"\bkp\s+walpole\b"),
    ("Philadelphia Hockey Club", r"\bphiladelphia\s+hockey\s+club\b|\bphc\b"),
    ("Philadelphia Jr. Flyers", r"\b(?:philadelphia|philly)?\s*j(?:unio)?r\.?\s+flyers\b"),
    ("Philadelphia Little Flyers", r"\b(?:philadelphia|philly)?\s*little\s+flyers\b"),
    ("Team Philadelphia", r"\bteam\s+philadelphia\b|\bteam\s+philly\b"),
    ("Valley Forge Minutemen", r"\b(?:valley\s+forge|vf)\s+minutemen\b"),
    ("Valley Forge Colonials", r"\bvalley\s+forge\s+colonials\b|\bvfc\b"),
    ("Hollydell Hurricanes", r"\bhollydell\s+hurricanes\b"),
    ("Hollydell Jr. Rebels", r"\b(?:hollydell\s+)?j(?:unio)?r\.?\s+rebels\b"),
    ("Philadelphia Resistance", r"\bphiladelphia\s+resistance\b"),
    ("Lehigh Valley Phantoms", r"\b(?:lehigh\s+valley|lv)\s+phantoms\b|\bphantoms\s+youth\b"),
    ("Hershey Jr. Bears", r"\bhershey\s+j(?:unio)?r\.?\s+bears\b"),
    ("Palmyra Black Knights", r"\bpalmyra\b|\bblack\s+knights\b"),
    ("WB/Scranton Jr. Knights", r"\b(?:wb/?scranton|wilkes[- ]barre)\s+j(?:unio)?r\.?\s+knights\b|\bj(?:unio)?r\.?\s+knights\b"),
    ("Grundy Senators", r"\bgrundy\s+senators\b"),
    ("Genesis Hockey Club", r"\bgenesis\s+hockey\b"),
    ("Chester County", r"\bchester\s+county\b"),
    ("Delco Phantoms", r"\bdelco\s+phantoms\b"),
    ("PA Quakers", r"\bpa\s+quakers\b|\bquakers\s+hockey\b"),
    ("Exton Kings", r"\bexton\s+kings\b"),
    ("Warwick Wildcats", r"\bwarwick\s+wildcats\b"),
    ("Haverford Hawks", r"\bhaverford\s+hawks\b"),
    ("Bucks County Admirals", r"\bbucks\s+county\s+admirals\b|\badmirals\s+hockey\b"),
    ("Keystone Kraken", r"\bkeystone\s+kraken\b"),
    ("North Jersey Avalanche", r"\b(?:north\s+jersey|nj)\s+avalanche\b"),
    ("New Jersey Colonials", r"\b(?:new\s+jersey|nj)\s+colonials\b"),
    ("New Jersey Devils Youth", r"\b(?:new\s+jersey|nj)\s+devils(?:\s+youth)?\b|\bdevils\s+youth\b"),
    ("New Jersey Jr. Titans", r"\b(?:new\s+jersey|nj)\s+(?:j(?:unio)?r\.?\s+)?titans\b|\bj(?:unio)?r\.?\s+titans\b"),
    ("New Jersey Rockets", r"\b(?:new\s+jersey|nj)\s+rockets\b|\brockets?\s+hockey\s+clubs?\b"),
    ("Red Bank Generals", r"\bred\s+bank\s+generals?\b"),
    ("Woodbridge Wolfpack", r"\bwoodbridge\s+wolfpack\b"),
    ("Mercer Chiefs", r"\bmercer\s+chiefs\b"),
    ("Jersey Hitmen", r"\b(?:jersey|nj)\s+hitmen\b"),
    ("Jersey Colts", r"\bjersey\s+colts\b"),
    ("NJ Bandits", r"\b(?:new\s+jersey|nj)\s+bandits\b"),
    ("NJ Jets", r"\b(?:new\s+jersey|nj)\s+jets\b"),
    ("NJ Bears", r"\b(?:new\s+jersey|nj)\s+bears\b"),
    ("Bridgewater Bears", r"\bbridgewater\s+bears\b"),
    ("Skyland Kings", r"\bskylands?\s+(?:jr\.?\s+)?kings\b"),
    ("Montclair Blues", r"\bmontclair\s+blues\b"),
    ("Igloo Jaguars", r"\bigloo\s+jaguars\b"),
    ("Jersey Shore Wildcats", r"\bjersey\s+shore\s+wildcats\b"),
    ("Jersey Shore Whalers", r"\bjersey\s+shore\s+whalers\b"),
    ("Brick Hockey Club", r"\bbrick\s+hockey\s+club\b"),
    ("Toms River Blackhawks", r"\btoms\s+river\s+blackhawks\b"),
    ("Princeton Tiger Lilies", r"\btiger\s+lilies\b"),
    ("Princeton Youth Hockey", r"\bprinceton\s+youth\s+hockey\b"),
    ("Long Island Gulls", r"\b(?:long\s+island|li)\s+gulls\b"),
    ("Long Island Royals", r"\b(?:long\s+island|li)\s+royals\b|\broyals\s+hockey\s+club\b"),
    ("Long Island Sharks", r"\b(?:long\s+island|li)\s+sharks\b"),
    ("Long Island Arrows", r"\b(?:long\s+island|li|ny)\s+arrows\b"),
    ("Long Island Rebels", r"\b(?:long\s+island|li)\s+rebels\b"),
    ("Team Long Island", r"\bteam\s+long\s+island\b"),
    ("PAL Jr. Islanders", r"\bp\.?a\.?l\.?\s+(?:j(?:unio)?r\.?\s+)?islanders\b"),
    ("Palisades Predators", r"\bpalisades\s+predators\b"),
    ("Ramapo Saints", r"\bramapo\s+saints\b"),
    ("New York Saints", r"\b(?:new\s+york|ny)\s+saints\b"),
    ("Brewster Bulldogs", r"\bbrewster\s+bulldogs\b"),
    ("Peconic Wildcats", r"\bpeconic\s+wildcats\b"),
    ("Westchester Express", r"\bwestchester\s+express\b"),
    ("Westchester Vipers", r"\bwestchester\s+vipers\b"),
    ("Sound Tigers", r"\bsound\s+tigers\b|\bct\s+j(?:unio)?r\.?\s+rangers\b|\bblue\s+devil\s+rangers\b"),
    ("Mid-Fairfield", r"\bmid[- ]?fairfield\b|\bmf\s+rangers\b"),
    ("CT Ice Cats", r"\bct\s+ice\s+cats\b|\bice\s+cats\b"),
    ("Danbury Hat Tricks", r"\bdanbury\s+hat\s+tricks\b|\bhat\s+tricks\b"),
    ("Elite Hockey", r"\belite\s+hockey\s+(?:academy|program|premier|elite)\b"),
    ("Washington Little Capitals", r"\b(?:washington\s+)?little\s+cap(?:ital)?s\b"),
    ("Ashburn Xtreme", r"\bashburn\s+xtreme\b"),
    ("Ashburn Rebels", r"\bashburn\s+rebels\b"),
    ("Syracuse Valley Eagles", r"\bsyracuse\s+(?:valley\s+)?eagles\b|\bvalley\s+eagles\b"),
    ("Utica Jr. Comets", r"\b(?:utica\s+)?j(?:unio)?r\.?\s+comets\b"),
    ("Rochester Jr. Americans", r"\brochester\s+j(?:unio)?r\.?\s+(?:americans|amerks)\b|\bj(?:unio)?r\.?\s+amerks\b"),
    ("Bishop Kearney", r"\bbishop\s+kearney\b|\bbk\s+selects\b"),
    ("CP Dynamo", r"\bcp\s+dynamo\b"),
    ("Buffalo Jr. Sabres", r"\bbuffalo\s+j(?:unio)?r\.?\s+sabres\b"),
    ("Buffalo Regals", r"\bbuffalo\s+regals\b"),
    ("Skaneateles Lakers", r"\bskaneateles(?:\s+lakers)?\b"),
    ("Vermont Jr. Cats", r"\bvermont\s+j(?:unio)?r\.?\s+cats\b"),
    ("Stateline Hawks", r"\bstateline\s+hawks\b"),
]

GENERIC_STOP_CLUBS = {
    "hockey", "hockey club", "youth hockey", "selects", "elite", "premier",
    "black", "white", "red", "blue", "gold", "green", "orange", "silver", "grey", "gray",
    "coyotes", "vikings", "seahawks", "bishops", "blazers", "bulldogs", "eagles",
    "flyers", "rangers", "bruins", "devils", "islanders", "capitals", "knights",
    "hawks", "wolves", "bears", "panthers", "tigers", "lions", "warriors",
    "spartans", "generals", "rebels", "saints", "royals", "kings", "stars",
    "phantoms", "wildcats", "whalers", "penguins", "falcons", "hornets",
    "hingham", "milton", "braintree", "franklin", "walpole", "tri-county",
    "allegedly", "bourget", "stanstead", "nichols", "gilmour", "shadyside",
}


def resolve_club_rule(team_name: str) -> tuple[str, str] | None:
    """Return (club_display_name, regex_pattern) for a schedule team name."""
    raw = (team_name or "").strip()
    if not raw:
        return None
    for club_name, pattern in CLUB_RULES:
        if re.search(pattern, raw, re.I):
            return club_name, pattern

    # Fallback: extract distinctive multi-word organization name
    cleaned = re.sub(r"^(?:\d{1,2}[A-Z0-9]{1,3}\s*-\s*|[Uu]\d{1,2}\s+|\d{1,2}[Uu]\s+)", "", raw)
    cleaned = re.sub(r"\b202\d[/-]202\d\b", " ", cleaned)
    cleaned = re.split(
        r"\s+-\s+|\b(?:[Uu]\s*\d{1,2}|\d{1,2}\s*(?:[Uu]|[Oo]|AAA|AA|A|GRY|L|INT|BEG|ADV|M)\b|20[01]\d\b|Tier\s*[1234I]+|Elite|Premier|National|American|Gold|Upper\s+Gold|Platinum|Major|Minor|Mite|Squirt|Pee\s*Wee|Bantam|Midget|Girls|Boys)\b",
        cleaned,
        maxsplit=1,
        flags=re.I,
    )[0]
    cleaned = re.sub(
        r"\b(?:Black|White|Red|Blue|Gold|Green|Orange|Silver|Grey|Gray|Navy|Maroon|Purple|Yellow)\s*$",
        "",
        cleaned.strip(),
        flags=re.I,
    )
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -()/.,")
    words = cleaned.split()
    if len(words) >= 2 and len(cleaned) >= 8 and cleaned.lower() not in GENERIC_STOP_CLUBS:
        return cleaned, rf"\b{re.escape(cleaned)}\b"
    return None


# ---------------------------------------------------------------------------
# Schedule Fetchers for the 5 Sites
# ---------------------------------------------------------------------------

def fetch_ayhl_games(shoots_by_day: dict[str, list[dict]]) -> list[dict]:
    """Site 1: https://atlantichockey.org/ayhl.php (full_schedule.php)."""
    games: list[dict] = []
    try:
        resp = requests.get("https://atlantichockey.org/full_schedule.php", headers=USER_AGENT, timeout=25)
        resp.raise_for_status()
        # AYHL retains a deprecated <td> inside an HTML comment. Regexes must
        # not count it as a live cell or the date/time column shifts.
        html = re.sub(r"<!--.*?-->", "", resp.text, flags=re.S)
        m_tbl = re.search(
            r'<table[^>]*id="schedule_table"[^>]*class="w3-hide-small"[^>]*>(.*?)</table>',
            html,
            re.S | re.I,
        )
        if not m_tbl:
            raise ValueError("schedule table not found")
        table_html = m_tbl.group(1)
        parsed_rows = 0
        for tr_html in re.findall(r"<tr[^>]*>(.*?)</tr>", table_html, re.S | re.I):
            tds = [
                _html_to_text(td, sep=" ").strip()
                for td in re.findall(r"<td[^>]*>(.*?)</td>", tr_html, re.S | re.I)
            ]
            if len(tds) < 8:
                continue
            game_num = tds[0].split()[0] if tds[0] else ""
            level = tds[1]
            away = tds[2]
            home = tds[4]
            loc = tds[6]
            dt_str = tds[7]
            m = re.search(r"(\d{2}/\d{2}/\d{2})\s+(\d{1,2}:\d{2}\s*[AP]M)", dt_str, re.I)
            if not m:
                continue
            dt_obj = datetime.strptime(m.group(1) + " " + m.group(2).upper(), "%m/%d/%y %I:%M %p")
            parsed_rows += 1
            day_iso = dt_obj.date().isoformat()
            if day_iso not in shoots_by_day:
                continue
            rink = canonical_rink(loc, "ayhl")
            if not rink:
                continue
            games.append({
                "date": day_iso,
                "time": dt_obj.strftime("%-I:%M %p"),
                "sort_time": dt_obj.strftime("%H:%M"),
                "away": away,
                "home": home,
                "division": level,
                "rink": rink,
                "published_venue": loc,
                "site": "AYHL",
                "source_url": f"https://atlantichockey.org/game_preview.php?gameid={game_num}" if game_num.isdigit() else "https://atlantichockey.org/full_schedule.php",
            })
        if not parsed_rows:
            raise ValueError("no schedule rows could be parsed")
    except Exception as exc:
        raise ScheduleResearchError(f"AYHL schedule failed ({type(exc).__name__}); refresh aborted.") from exc
    return games


def fetch_ahf_thf_games(shoots_by_day: dict[str, list[dict]]) -> list[dict]:
    """Sites 3 & 4: AHF (atlantichockeyfederation.com) & THF (tier1hockeyfederation.com) via TimeToScore."""
    games: list[dict] = []
    try:
        resp = requests.get("https://atlantichockeyfederation.com/schedule/", headers=USER_AGENT, timeout=20)
        resp.raise_for_status()
        m_sec = re.search(r'data-api-secret="([^"]+)"', resp.text)
        if not m_sec:
            raise ValueError("public API configuration not found")
        secret = codecs.decode(m_sec.group(1), "rot13")

        def fetch_day(day_iso: str) -> list[dict]:
            params = {
                "auth_key": "web",
                "auth_timestamp": str(int(time.time())),
                "body_md5": hashlib.md5(b"").hexdigest(),
                "league_id": "0",
                "season_id": "0",
                "start": day_iso,
                "end": day_iso,
            }
            query = urlencode(sorted(params.items()), safe="~")
            sig = hmac.new(
                secret.encode(),
                ("GET\n/get_schedule_lite\n" + query).encode(),
                hashlib.sha256,
            ).hexdigest()
            url = f"https://api.blackbear.timetoscore.com/get_schedule_lite?{query}&auth_signature={sig}"
            r = requests.get(url, headers=USER_AGENT, timeout=20)
            r.raise_for_status()
            payload = r.json()
            if not isinstance(payload, dict) or not isinstance(payload.get("games"), list):
                raise ValueError("invalid schedule response")
            return payload["games"]

        for day_iso in sorted(shoots_by_day):
            for row in fetch_day(day_iso):
                if row.get("date") != day_iso:
                    continue
                status = (row.get("game_status") or "").lower()
                if status in {"cancelled", "canceled"}:
                    continue
                loc = row.get("location") or ""
                lid = str(row.get("league_id") or "")
                site_label = "AHF" if lid == "4" else "THF" if lid == "5" else "AHF / BlackBear Showcase"
                site_base = (
                    "https://www.tier1hockeyfederation.com/schedule/"
                    if lid == "5"
                    else "https://atlantichockeyfederation.com/schedule/"
                )
                rink = canonical_rink(loc, site_label)
                if not rink:
                    continue
                raw_t = str(row.get("time") or "")[:5]
                disp_time = raw_t
                if re.match(r"^\d{2}:\d{2}$", raw_t):
                    disp_time = datetime.strptime(raw_t, "%H:%M").strftime("%-I:%M %p")
                ref = urlencode({
                    "game": row.get("game_id", ""),
                    "league": lid,
                    "season": row.get("season_id", "0"),
                })
                games.append({
                    "date": day_iso,
                    "time": disp_time,
                    "sort_time": raw_t or "99:99",
                    "away": (row.get("away_team") or "").strip(),
                    "home": (row.get("home_team") or "").strip(),
                    "division": (row.get("division_name") or row.get("level") or "").strip(),
                    "rink": rink,
                    "published_venue": loc,
                    "site": site_label,
                    "source_url": f"{site_base}?{ref}",
                })
    except Exception as exc:
        raise ScheduleResearchError(f"AHF/THF schedule failed ({type(exc).__name__}); refresh aborted.") from exc
    return games


GAMESHEET_SEASONS = [
    # Site 5: FedHockey / Eastern Hockey Federation
    (15008, "FedHockey (EHF)", {"NE"}),
    (14728, "FedHockey (EHF Parity)", {"NE"}),
    # Site 2: GameSheet active 2026-2027 regional leagues & tournaments
    (15279, "GameSheet (PHL New England)", {"NE"}),
    (15207, "GameSheet (NE Girls Fall)", {"NE"}),
    (15208, "GameSheet (NE Girls Winter)", {"NE"}),
    (15343, "GameSheet (South Shore Conf)", {"NE"}),
    (15344, "GameSheet (South Shore Conf)", {"NE"}),
    (15345, "GameSheet (South Shore Conf)", {"NE"}),
    (15394, "GameSheet (South Shore Conf)", {"NE"}),
    (15153, "GameSheet (South Shore Classics)", {"NE"}),
    (15566, "GameSheet (Valley Premier)", {"NE"}),
    (15306, "GameSheet (Granite State)", {"NE"}),
    (15307, "GameSheet (Granite State)", {"NE"}),
    (15536, "GameSheet (CT Hockey Conf)", {"NE", "NY"}),
    (16277, "GameSheet (Boston Chowda Puck)", {"NE"}),
    (15381, "GameSheet (Long Island AHL)", {"NY"}),
    (15368, "GameSheet (Hudson Valley HSHL)", {"NY"}),
    (16053, "GameSheet (Empire State HL)", {"NY"}),
    (16214, "GameSheet (Rochester Prep Invite)", {"NY"}),
    (16267, "GameSheet (Empire State Showdown)", {"NY"}),
    (15892, "GameSheet (Rochester Ice Center)", {"NY"}),
    (16201, "GameSheet (Syracuse Showcase)", {"NY"}),
    (16233, "GameSheet (Utica Comets Cup)", {"NY"}),
    (15430, "GameSheet (NJYHL)", {"MID", "NY"}),
    (15111, "GameSheet (DVHL)", {"MID"}),
    (15431, "GameSheet (Mid-Atlantic HA)", {"MID"}),
    (15860, "GameSheet (Mid-Atlantic Prep)", {"MID", "NE", "NY"}),
    (15862, "GameSheet (Mid-Atlantic Girls)", {"MID"}),
    (16150, "GameSheet (Mid-Atlantic Showcase)", {"MID"}),
    (15085, "GameSheet (AAHA Girls Tier I)", {"MID"}),
    (15086, "GameSheet (AAHA Girls Tier II)", {"MID"}),
    (15483, "GameSheet (Northeast Pack)", {"MID", "NY", "NE"}),
    (15572, "GameSheet (CPIHL)", {"MID"}),
]


def fetch_gamesheet_payload(day_iso: str, sid: int, public_python: str | None = None) -> dict:
    """Fetch public schedules without mixing the Django and public HTTP runtimes."""
    if public_python:
        env = {k: v for k, v in os.environ.items() if k in {"PATH", "HOME", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR"}}
        try:
            result = subprocess.run(
                [public_python, str(Path(__file__).with_name("public_schedule_worker.py"))],
                input=json.dumps({"date": day_iso, "season": sid}),
                capture_output=True, text=True, timeout=30, env=env,
            )
            if result.returncode:
                raise ValueError("public schedule worker failed")
            envelope = json.loads(result.stdout)
            status = envelope["status_code"]
            payload = envelope["payload"]
        except (OSError, subprocess.SubprocessError, ValueError, KeyError) as exc:
            raise ScheduleResearchError(f"GameSheet public runtime failed for season {sid} on {day_iso}; refresh aborted.") from exc
    else:
        response = requests.get(
            f"https://gamesheetstats.com/api/unified-games/{sid}",
            params={"dateStart": day_iso, "dateEnd": day_iso}, timeout=12,
        )
        status = response.status_code
        payload = response.json() if status == 200 else None
    if status != 200:
        raise ScheduleResearchError(f"GameSheet season {sid} on {day_iso}: HTTP {status}; refresh aborted.")
    return payload


def fetch_gamesheet_and_fedhockey_games(shoots_by_day: dict[str, list[dict]], public_python: str | None = None) -> list[dict]:
    """Sites 2 & 5: GameSheet (gamesheetinc.com) and FedHockey (fedhockey.com)."""
    games: list[dict] = []
    tasks: list[tuple[str, int, str]] = []
    for day_iso, shoots in sorted(shoots_by_day.items()):
        day_regions = {RINK_REGION.get(s["rink"]) for s in shoots if s.get("rink")}
        for sid, label, regions in GAMESHEET_SEASONS:
            if day_regions & regions:
                tasks.append((day_iso, sid, label))

    def _fetch_one(task: tuple[str, int, str]) -> list[dict]:
        day_iso, sid, label = task
        found: list[dict] = []
        try:
            payload = fetch_gamesheet_payload(day_iso, sid, public_python=public_python)
            if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                raise ValueError("missing game data")
            rows = payload["data"]
            expected = (payload.get("meta") or {}).get("filtered")
            if expected is not None and int(expected) != len(rows):
                raise ValueError("incomplete game response")
            for g in rows:
                loc = g.get("location") or ""
                rink = canonical_rink(loc, label)
                if not rink:
                    continue
                raw_time = (g.get("time") or "").strip()
                sort_t = "99:99"
                if raw_time:
                    try:
                        sort_t = datetime.strptime(raw_time.upper(), "%I:%M %p").strftime("%H:%M")
                    except ValueError:
                        sort_t = raw_time[:5]
                visitor = (g.get("visitor") or {}).get("title") or ""
                home = (g.get("home") or {}).get("title") or ""
                div = ((g.get("home") or {}).get("division") or {}).get("title") or ""
                if not visitor or not home:
                    continue
                found.append({
                    "date": day_iso,
                    "time": raw_time or "TBD",
                    "sort_time": sort_t,
                    "away": visitor.strip(),
                    "home": home.strip(),
                    "division": div.strip(),
                    "rink": rink,
                    "published_venue": loc,
                    "site": label,
                    "source_url": f"https://gamesheetstats.com/seasons/{sid}/games",
                })
        except ScheduleResearchError:
            raise
        except Exception as exc:
            raise ScheduleResearchError(
                f"GameSheet season {sid} on {day_iso} failed ({type(exc).__name__}); refresh aborted."
            ) from exc
        return found

    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(_fetch_one, t) for t in tasks]
        for fut in as_completed(futures):
            games.extend(fut.result())
    return games


def fetch_hollydell_facility_games(shoots_by_day: dict[str, list[dict]]) -> list[dict]:
    """Supplement Hollydell days with Hollydell's DaySmart facility calendar."""
    games: list[dict] = []
    hollydell_days = [
        d for d, shoots in shoots_by_day.items()
        if any(s.get("rink") == "Hollydell Ice Arena" for s in shoots)
    ]
    for day_iso in hollydell_days:
        page = 1
        while page <= 10:
            try:
                params = {
                    "company": "hollydell",
                    "page[size]": "50",
                    "page[number]": str(page),
                    "sort": "start",
                    "filter[start__gte]": f"{day_iso} 00:00:00",
                    "filter[start__lte]": f"{day_iso} 23:59:59",
                    "filter[resource.facility.my_sam_visible]": "true",
                    "filter[eventType.code__not]": "L",
                    "include": "homeTeam.league.programType,visitingTeam.league.programType,summary,resource.facility,eventType",
                }
                r = requests.get("https://api.daysmartrecreation.com/v1/events", params=params, timeout=15)
                r.raise_for_status()
                payload = r.json()
                if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                    raise ValueError("missing event data")
                rows = payload["data"]
                included = {(i["type"], str(i["id"])): i for i in payload.get("included") or []}

                def rel(item, name):
                    ref = (item.get("relationships") or {}).get(name, {}).get("data")
                    return included.get((ref["type"], str(ref["id"])), {}) if isinstance(ref, dict) else {}

                for row in rows:
                    attr = row.get("attributes") or {}
                    kind = (rel(row, "eventType").get("attributes") or {}).get("name", "")
                    if not re.search(r"\bgame\b", kind, re.I):
                        continue
                    resource = rel(row, "resource")
                    fac = (rel(resource, "facility").get("attributes") or {}).get("name", "")
                    if "hollydell" not in fac.lower():
                        continue
                    start_dt = datetime.fromisoformat(attr["start"])
                    if start_dt.date().isoformat() != day_iso:
                        continue
                    home = (rel(row, "homeTeam").get("attributes") or {}).get("name", "").strip()
                    away = (rel(row, "visitingTeam").get("attributes") or {}).get("name", "").strip()
                    desc = str(attr.get("desc") or "").strip()
                    pair = re.split(r"\s+vs\.?\s+", desc, maxsplit=1, flags=re.I)
                    if len(pair) == 2:
                        home, away = pair[0].strip(), pair[1].strip()
                    if not home or not away:
                        continue
                    sheet = (resource.get("attributes") or {}).get("name", "")
                    games.append({
                        "date": day_iso,
                        "time": start_dt.strftime("%-I:%M %p"),
                        "sort_time": start_dt.strftime("%H:%M"),
                        "away": away,
                        "home": home,
                        "division": "",
                        "rink": "Hollydell Ice Arena",
                        "published_venue": f"{fac} - {sheet}".strip(" -"),
                        "site": "Hollydell Rink Calendar",
                        "source_url": "https://apps.daysmartrecreation.com/dash/x/#/online/hollydell/calendar",
                    })
                meta_page = (payload.get("meta") or {}).get("page") or {}
                if page >= int(meta_page.get("last-page") or 1):
                    break
                page += 1
            except Exception as exc:
                raise ScheduleResearchError(
                    f"Hollydell calendar on {day_iso} failed ({type(exc).__name__}); refresh aborted."
                ) from exc
        else:
            raise ScheduleResearchError(f"Hollydell calendar on {day_iso} exceeded pagination limit; refresh aborted.")
    return games


# ---------------------------------------------------------------------------
# Optional Local Muse Spark + Browser Agent Pass
# ---------------------------------------------------------------------------

def run_muse_spark_agent_for_day(day_iso: str, rinks: list[str]) -> list[dict]:
    """
    Launch a separate Muse Spark worker with websearch/webfetch and an isolated
    Playwright browser. A failed AI pass aborts the whole refresh.
    """
    model = os.environ.get("PUCKPRO_RESEARCH_MODEL", "opencode/muse-spark-1.3")
    system_prompt = (
        "You are a hockey schedule researcher. Search the 5 target sites: "
        "1) https://atlantichockey.org/ayhl.php "
        "2) https://www.gamesheetinc.com/ "
        "3) https://atlantichockeyfederation.com/ "
        "4) https://www.tier1hockeyfederation.com/ "
        "5) https://fedhockey.com/ "
        "Find every hockey game played on the specified date at the specified rinks. "
        "Return ONLY valid JSON: {\"games\": [{\"date\": \"YYYY-MM-DD\", \"time\": \"HH:MM AM/PM\", "
        "\"away\": \"...\", \"home\": \"...\", \"division\": \"...\", \"rink\": \"...\", "
        "\"published_venue\": \"...\", \"site\": \"...\", \"source_url\": \"https://...\"}]}"
    )
    with tempfile.TemporaryDirectory(prefix="muse-opp-") as scratch:
        config = {
            "mcp": {"servers": {"rinkbrowser": {
                "type": "local",
                "codemode": False,
                "command": ["npx", "--yes", "@playwright/mcp@0.0.83", "--headless", "--isolated", "--no-webmcp", "--block-service-workers"],
            }}},
            "agents": {
                "rink-research": {
                    "mode": "primary",
                    "steps": 25,
                    "system": system_prompt,
                    "permissions": [
                        {"action": "*", "resource": "*", "effect": "deny"},
                        {"action": "websearch", "resource": "*", "effect": "allow"},
                        {"action": "webfetch", "resource": "https://*", "effect": "allow"},
                        *[
                            {"action": f"rinkbrowser_{tool}", "resource": "*", "effect": "allow"}
                            for tool in ("browser_navigate", "browser_snapshot", "browser_click", "browser_select_option", "browser_wait_for", "browser_tabs", "browser_navigate_back", "browser_close")
                        ],
                    ],
                }
            }
        }
        browser = os.environ.get("OPPORTUNITIES_BROWSER_EXECUTABLE")
        if not browser:
            cached = list((Path.home() / ".cache" / "ms-playwright").glob("chromium-*/chrome-linux*/chrome"))
            browser = str(max(cached, key=lambda p: p.stat().st_mtime)) if cached else shutil.which("chromium")
        if browser:
            config["mcp"]["servers"]["rinkbrowser"]["command"].extend(["--executable-path", browser])
        Path(scratch, "opencode.json").write_text(json.dumps(config))
        # Resolve this worker's config independently of the calling session.
        # Both the session marker and logical working directory are inherited
        # from OpenCode's shell, even when Popen has a different cwd.
        env = {
            k: v for k, v in os.environ.items()
            if k not in {"OPENCODE_SESSION_ID", "OPENCODE", "OPENCODE_TERMINAL"}
            and not k.startswith(("PIPEDRIVE_", "FACEBOOK_", "GOOGLE_", "CLICKUP_", "RAILWAY_"))
            and k not in {"SECRET_KEY", "OPPORTUNITIES_SYNC_SECRET", "DATABASE_URL"}
        }
        env["PWD"] = scratch
        msg = json.dumps({"date": day_iso, "rinks": rinks, "sites": [s["url"] for s in TARGET_SITES]})
        proc = subprocess.Popen(
            ["opencode", "run", "--standalone", "--agent", "rink-research", "--model", model, "--format", "json", msg],
            cwd=scratch,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            stdout, _ = proc.communicate(timeout=300)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate()
            raise ScheduleResearchError(f"Muse timed out on {day_iso}; refresh aborted.")
        texts = []
        for line in stdout.splitlines():
            try:
                ev = json.loads(line)
                if ev.get("type") == "error":
                    error = ev.get("error") or {}
                    if error.get("type") == "provider.auth":
                        raise ScheduleResearchError(
                            f"Muse authentication failed on {day_iso}; check the OpenCode provider API key. Refresh aborted."
                        )
                    raise ScheduleResearchError(f"Muse reported an error on {day_iso}; refresh aborted.")
                if ev.get("type") == "text":
                    texts.append(ev.get("part", {}).get("text", ""))
            except json.JSONDecodeError:
                continue
        if proc.returncode != 0:
            raise ScheduleResearchError(f"Muse failed on {day_iso} (exit {proc.returncode}); refresh aborted.")
        raw = next((t.strip() for t in reversed(texts) if t.strip()), "")
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict) or not isinstance(payload.get("games"), list):
                raise ValueError("missing games array")
            games = payload["games"]
            required = {"date", "time", "away", "home", "rink", "source_url"}
            for game in games:
                if not isinstance(game, dict) or not required.issubset(game):
                    raise ValueError("invalid game record")
                if any(not isinstance(game[k], str) or not game[k].strip() for k in required):
                    raise ValueError("invalid game field")
                if game["date"] != day_iso or not game["source_url"].startswith("https://"):
                    raise ValueError("wrong date or missing public source")
            return games
        except (ValueError, TypeError) as exc:
            raise ScheduleResearchError(f"Muse returned invalid results on {day_iso}; refresh aborted.") from exc


# ---------------------------------------------------------------------------
# Main Research & CRM Matching Pipeline
# ---------------------------------------------------------------------------

def rink_coverage_warnings(rinks: list[str]) -> list[str]:
    warnings = []
    for rink in sorted(set(rinks)):
        region = RINK_REGION.get(rink)
        if region == "ROLLER":
            warnings.append(f"{rink}: not covered by the five ice-hockey sources; separate roller-hockey research is required.")
        elif not region:
            warnings.append(f"{rink}: unmapped facility; game coverage is incomplete.")
    return warnings


def build_game_opportunities_report(target_date: str | None = None, use_muse: bool = False, public_python: str | None = None, use_luna: bool = False, progress=None) -> dict:
    if use_muse and use_luna:
        raise ScheduleResearchError("Select one AI research provider, not both Muse and Luna.")
    _enable_ipv4_fastpath()
    token, domain = get_pipedrive_credentials()
    tz = ZoneInfo("America/New_York")
    today_iso = datetime.now(tz).date().isoformat()

    # 1. Fetch all Pipedrive data (entire CRM + undone shoot_booking activities)
    with ThreadPoolExecutor(max_workers=4) as pool:
        f_acts = pool.submit(pipedrive_get_all, token, domain, "activities", user_id=0, type="shoot_booking", done=0)
        f_deals = pool.submit(pipedrive_get_all, token, domain, "deals", status="all")
        f_persons = pool.submit(pipedrive_get_all, token, domain, "persons")
        f_leads = pool.submit(pipedrive_get_all, token, domain, "leads", archived_status="not_archived")
        f_notes = pool.submit(pipedrive_get_all, token, domain, "notes")
        activities = f_acts.result()
        deals = f_deals.result()
        persons = f_persons.result()
        leads = f_leads.result()
        notes = f_notes.result()

    # Index notes by deal, person, lead
    notes_by_deal: dict[str, list[str]] = {}
    notes_by_person: dict[str, list[str]] = {}
    notes_by_lead: dict[str, list[str]] = {}
    for n in notes:
        plain = _html_to_text(n.get("content") or "", sep="\n")
        if not plain:
            continue
        if n.get("deal_id"):
            notes_by_deal.setdefault(str(n["deal_id"]), []).append(plain)
        if n.get("person_id"):
            notes_by_person.setdefault(str(n["person_id"]), []).append(plain)
        if n.get("lead_id"):
            notes_by_lead.setdefault(str(n["lead_id"]), []).append(plain)

    persons_by_id = {str(p["id"]): p for p in persons}
    deals_by_id = {str(d["id"]): d for d in deals}

    # 2. Group upcoming shoots day by day
    shoots_by_day: dict[str, list[dict]] = {}
    for a in activities:
        if a.get("type") != "shoot_booking" or a.get("done"):
            continue
        dd = a.get("due_date") or ""
        dt = (a.get("due_time") or "").strip()
        if not dd:
            continue
        if not dt or dt in {"00:00", "00:00:00"}:
            local_date = dd
            local_time = "TBD"
            sort_time = "99:99"
        else:
            parts = dt.split(":")
            utc_dt = datetime.fromisoformat(f"{dd}T{parts[0]:0>2}:{parts[1]:0>2}:00").replace(tzinfo=timezone.utc)
            loc_dt = utc_dt.astimezone(tz)
            local_date = loc_dt.date().isoformat()
            local_time = loc_dt.strftime("%-I:%M %p")
            sort_time = loc_dt.strftime("%H:%M")

        if local_date < today_iso:
            continue
        if target_date and local_date != target_date:
            continue

        loc = a.get("location") or ""
        if isinstance(loc, dict):
            loc = loc.get("value") or loc.get("formatted_address") or ""
        rink = canonical_rink(loc)
        did = str(a.get("deal_id") or "")
        pid = str(a.get("person_id") or "")
        client_name = a.get("person_name") or a.get("deal_title") or ""
        client_notes = (notes_by_deal.get(did, []) if did else []) + (notes_by_person.get(pid, []) if pid else [])
        client_note_summary = _redact_pii(
            " · ".join(line.strip() for n_txt in client_notes for line in n_txt.splitlines() if line.strip())[:240]
        )
        crm_url = (
            f"https://{domain}.pipedrive.com/deal/{did}"
            if did
            else f"https://{domain}.pipedrive.com/person/{pid}"
            if pid
            else f"https://{domain}.pipedrive.com/activities/list"
        )
        shoots_by_day.setdefault(local_date, []).append({
            "activity_id": str(a.get("id") or ""),
            "date": local_date,
            "time": local_time,
            "sort_time": sort_time,
            "title": a.get("subject") or "Shoot Booking",
            "client_name": client_name,
            "client_note": client_note_summary,
            "location": loc,
            "rink": rink or loc,
            "deal_id": did,
            "person_id": pid,
            "crm_url": crm_url,
        })

    for d_list in shoots_by_day.values():
        d_list.sort(key=lambda x: (x["sort_time"], x["rink"]))

    # 3. Build full-CRM searchable index (all deals, active leads, and standalone persons with notes)
    # Prefer open deals first and deduplicate by person_id so a person with both an open
    # and a past deal appears once under their active/best deal.
    status_priority = {"open": 0, "won": 1, "lost": 2}
    deals_sorted = sorted(deals, key=lambda d: (status_priority.get(d.get("status") or "open", 3), -int(d.get("id") or 0)))

    crm_entries: list[dict] = []
    seen_persons: set[str] = set()
    for d in deals_sorted:
        did = str(d["id"])
        pid_raw = d.get("person_id")
        pid = str(pid_raw.get("value") if isinstance(pid_raw, dict) else (pid_raw or ""))
        if pid and pid in seen_persons:
            continue
        if pid:
            seen_persons.add(pid)
        p_obj = persons_by_id.get(pid, {})
        notes_list = notes_by_deal.get(did, []) + (notes_by_person.get(pid, []) if pid else [])
        full_text = "\n".join([d.get("title") or "", p_obj.get("name") or "", *notes_list]).strip()
        if not full_text:
            continue
        crm_entries.append({
            "kind": "deal",
            "id": did,
            "person_id": pid,
            "title": d.get("title") or p_obj.get("name") or f"Deal #{did}",
            "person_name": p_obj.get("name") or "",
            "status": d.get("status") or "open",
            "url": f"https://{domain}.pipedrive.com/deal/{did}",
            "full_text": full_text,
            "ages": extract_ages(full_text),
        })

    for l in leads:
        lid = str(l["id"])
        pid_raw = l.get("person_id")
        pid = str(pid_raw.get("value") if isinstance(pid_raw, dict) else (pid_raw or ""))
        if pid and pid in seen_persons:
            continue
        if pid:
            seen_persons.add(pid)
        p_obj = persons_by_id.get(pid, {})
        notes_list = notes_by_lead.get(lid, []) + (notes_by_person.get(pid, []) if pid else [])
        full_text = "\n".join([l.get("title") or "", p_obj.get("name") or "", *notes_list]).strip()
        if not full_text:
            continue
        crm_entries.append({
            "kind": "lead",
            "id": lid,
            "person_id": pid,
            "title": l.get("title") or p_obj.get("name") or f"Lead #{lid}",
            "person_name": p_obj.get("name") or "",
            "status": "open",
            "url": f"https://{domain}.pipedrive.com/leads/inbox/{lid}",
            "full_text": full_text,
            "ages": extract_ages(full_text),
        })

    for pid, p_obj in persons_by_id.items():
        if pid in seen_persons:
            continue
        notes_list = notes_by_person.get(pid, [])
        if not notes_list:
            continue
        full_text = "\n".join([p_obj.get("name") or "", *notes_list]).strip()
        crm_entries.append({
            "kind": "person",
            "id": pid,
            "person_id": pid,
            "title": p_obj.get("name") or f"Person #{pid}",
            "person_name": p_obj.get("name") or "",
            "status": "open",
            "url": f"https://{domain}.pipedrive.com/person/{pid}",
            "full_text": full_text,
            "ages": extract_ages(full_text),
        })

    # 4. Query all 5 schedule sites across our shoot days
    all_raw_games: list[dict] = []
    all_raw_games.extend(fetch_ayhl_games(shoots_by_day))
    all_raw_games.extend(fetch_ahf_thf_games(shoots_by_day))
    all_raw_games.extend(fetch_gamesheet_and_fedhockey_games(shoots_by_day, public_python=public_python))
    all_raw_games.extend(fetch_hollydell_facility_games(shoots_by_day))

    ai_day_results = {}
    if use_luna:
        from .luna_research import run_luna_research_for_day

        if progress:
            progress(f"Public schedules retrieved. Starting Luna/Codex browser research for {len(shoots_by_day)} shoot days...")
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = {}
            for day_iso, shoots in sorted(shoots_by_day.items()):
                rinks = sorted({s["rink"] for s in shoots if s.get("rink")})
                known = [g for g in all_raw_games if g["date"] == day_iso and g["rink"] in rinks]
                futures[pool.submit(run_luna_research_for_day, day_iso, rinks, known)] = day_iso
            for future in as_completed(futures):
                day_iso = futures[future]
                result = future.result()
                ai_day_results[day_iso] = result
                for game in result["games"]:
                    try:
                        game["sort_time"] = datetime.strptime(game["time"].upper(), "%I:%M %p").strftime("%H:%M")
                    except ValueError:
                        raise ScheduleResearchError(f"Luna returned an invalid game time on {day_iso}; refresh aborted.")
                    game["discovery"] = "Luna/Codex"
                    all_raw_games.append(game)
                if progress:
                    progress(f"Luna checked {day_iso}: {len(result['games'])} additional game candidates, {len(result['limitations'])} coverage notes ({len(ai_day_results)}/{len(shoots_by_day)} days).")

    if use_muse:
        for day_iso, shoots in sorted(shoots_by_day.items()):
            day_rinks = sorted({s["rink"] for s in shoots if s.get("rink")})
            for mg in run_muse_spark_agent_for_day(day_iso, day_rinks):
                rk = canonical_rink(mg.get("rink") or mg.get("published_venue") or "")
                if rk:
                    mg["rink"] = rk
                    mg.setdefault("sort_time", "99:99")
                    all_raw_games.append(mg)

    # 5. Assemble day-by-day report: shoots -> games at same rinks -> teams -> CRM matches (direct + maybe)
    days_output: list[dict] = []
    total_games_count = 0
    total_unique_teams: set[str] = set()
    total_direct_matches = 0
    total_maybe_matches = 0

    for day_iso in sorted(shoots_by_day):
        day_shoots = shoots_by_day[day_iso]
        day_rinks = {s["rink"] for s in day_shoots if s.get("rink")}
        shoots_by_rink: dict[str, list[dict]] = {}
        for s in day_shoots:
            shoots_by_rink.setdefault(s["rink"], []).append(s)

        # Deduplicate games at the same rink/time/teams
        day_games: list[dict] = []
        seen_game_keys: set[tuple] = set()
        for g in all_raw_games:
            if g.get("date") != day_iso or g.get("rink") not in day_rinks:
                continue
            key = (
                g["rink"],
                g.get("sort_time", ""),
                re.sub(r"\W+", "", g["away"].lower()),
                re.sub(r"\W+", "", g["home"].lower()),
            )
            if key in seen_game_keys:
                continue
            seen_game_keys.add(key)
            day_games.append(g)

        day_games.sort(key=lambda g: (g["rink"], g.get("sort_time", "99:99"), g["home"]))
        total_games_count += len(day_games)

        # Build list of all teams playing at our shoot rinks that day
        # Include division in squad key when not already part of the team name so
        # e.g. AYHL "Philadelphia Junior Flyers" (10U) and (16U) stay distinct squads.
        teams_map: dict[str, dict] = {}
        for g in day_games:
            div_str = (g.get("division") or "").strip()
            for side in ("away", "home"):
                raw_t = g[side].strip()
                if not raw_t or raw_t.lower() in {"tbd", "tba", "unknown"}:
                    continue
                if div_str and div_str.lower() not in raw_t.lower():
                    t_label = f"{raw_t} ({div_str})"
                else:
                    t_label = raw_t
                total_unique_teams.add(t_label)
                opponent = g["home"] if side == "away" else g["away"]
                info = teams_map.setdefault(
                    t_label,
                    {
                        "name": t_label,
                        "raw_name": raw_t,
                        "division": div_str,
                        "rink": g["rink"],
                        "site": g["site"],
                        "games": [],
                        "club": "",
                        "direct_matches_count": 0,
                        "maybe_matches_count": 0,
                    },
                )
                info["games"].append({
                    "time": g["time"],
                    "opponent": opponent,
                    "rink": g["rink"],
                    "published_venue": g.get("published_venue") or g["rink"],
                    "site": g["site"],
                    "source_url": g.get("source_url") or "",
                })

        # Match teams against the entire CRM in two passes:
        # Pass 1: Direct matches (club + matching age group / birth year)
        # Pass 2: Maybe matches (club match where age/level is unspecified or within 1 adjacent year)
        direct_matches: list[dict] = []
        maybe_matches: list[dict] = []
        seen_day_crm_team: set[tuple[str, str, str]] = set()
        booked_deal_ids = {s["deal_id"] for s in day_shoots if s.get("deal_id")}
        booked_person_ids = {s["person_id"] for s in day_shoots if s.get("person_id")}

        resolved_teams: list[tuple[str, dict, str, str, set[str]]] = []
        for t_name, t_info in sorted(teams_map.items()):
            rule = resolve_club_rule(t_info["raw_name"])
            if not rule:
                continue
            club_name, pattern = rule
            t_info["club"] = club_name
            team_ages = extract_ages(f"{t_info['raw_name']} {t_info['division']}")
            resolved_teams.append((t_name, t_info, club_name, pattern, team_ages))

        for pass_name in ("direct", "maybe"):
            for t_name, t_info, club_name, pattern, team_ages in resolved_teams:
                related_shoots = shoots_by_rink.get(t_info["rink"], day_shoots)
                for entry in crm_entries:
                    if (entry["kind"] == "deal" and entry["id"] in booked_deal_ids) or (
                        entry.get("person_id") and entry["person_id"] in booked_person_ids
                    ):
                        continue
                    dedup_key = (entry["kind"], entry["id"], club_name)
                    if dedup_key in seen_day_crm_team:
                        continue
                    m = re.search(pattern, entry["full_text"], re.I)
                    if not m:
                        continue

                    crm_ages = entry["ages"]
                    if pass_name == "direct":
                        if team_ages and crm_ages and (team_ages & crm_ages):
                            reason = f"Matches {club_name} + age group ({', '.join(sorted(team_ages & crm_ages)).upper()})"
                        else:
                            continue
                        match_type = "direct"
                    else:
                        if not crm_ages or not team_ages:
                            reason = f"Club match ({club_name}) — age/level not specified in CRM"
                        elif _ages_within_one_year(team_ages, crm_ages):
                            reason = (
                                f"Club match ({club_name}) — CRM notes {', '.join(sorted(crm_ages)).upper()}, "
                                f"game is {', '.join(sorted(team_ages)).upper()}"
                            )
                        else:
                            continue
                        match_type = "maybe"

                    seen_day_crm_team.add(dedup_key)
                    snippet_start = max(0, m.start() - 50)
                    snippet_end = min(len(entry["full_text"]), m.end() + 90)
                    snippet = _redact_pii(" ".join(entry["full_text"][snippet_start:snippet_end].split()))

                    match_record = {
                        "match_type": match_type,
                        "reason": reason,
                        "club": club_name,
                        "team": t_name,
                        "division": t_info["division"],
                        "rink": t_info["rink"],
                        "games": t_info["games"],
                        "crm_kind": entry["kind"],
                        "crm_id": entry["id"],
                        "crm_title": entry["title"],
                        "crm_person": entry["person_name"],
                        "crm_status": entry["status"],
                        "crm_url": entry["url"],
                        "evidence": snippet,
                        "our_shoots": [
                            {
                                "time": s["time"],
                                "title": s["title"],
                                "client_name": s["client_name"],
                                "rink": s["rink"],
                                "crm_url": s["crm_url"],
                            }
                            for s in related_shoots
                        ],
                    }
                    if match_type == "direct":
                        direct_matches.append(match_record)
                        t_info["direct_matches_count"] += 1
                    else:
                        maybe_matches.append(match_record)
                        t_info["maybe_matches_count"] += 1

        # Sort matches so open deals/leads appear first
        status_rank = {"open": 0, "won": 1, "lost": 2}
        direct_matches.sort(key=lambda r: (status_rank.get(r["crm_status"], 3), r["crm_title"].lower()))
        maybe_matches.sort(key=lambda r: (status_rank.get(r["crm_status"], 3), r["crm_title"].lower()))

        total_direct_matches += len(direct_matches)
        total_maybe_matches += len(maybe_matches)

        dt_label = datetime.fromisoformat(day_iso).strftime("%A, %b %-d, %Y")
        days_output.append({
            "date": day_iso,
            "date_label": dt_label,
            "shoots": day_shoots,
            "rinks": sorted(day_rinks),
            "coverage_warnings": rink_coverage_warnings(list(day_rinks)) + ai_day_results.get(day_iso, {}).get("limitations", []),
            "games": day_games,
            "teams": sorted(teams_map.values(), key=lambda t: (-t["direct_matches_count"], -t["maybe_matches_count"], t["name"].lower())),
            "direct_matches": direct_matches,
            "maybe_matches": maybe_matches,
        })

    report = {
        "generated_at": datetime.now(tz).strftime("%Y-%m-%d %-I:%M %p %Z"),
        "generated_iso": datetime.now(timezone.utc).isoformat(),
        "sites": TARGET_SITES,
        "coverage": {
            "muse_completed": use_muse,
            "ai_completed": use_muse or use_luna,
            "ai_model": "openai/gpt-6-luna (Codex)" if use_luna else "Muse Spark" if use_muse else "none",
            "ai_days_checked": len(ai_day_results) if use_luna else len(shoots_by_day) if use_muse else 0,
            "scope": "Configured public league seasons and mapped facilities; not a guarantee of exhaustive coverage.",
            "crm_scope": "One prioritized deal/lead per person plus standalone persons with notes; archived leads and all custom fields are not indexed.",
        },
        "summary": {
            "shoot_days": len(days_output),
            "upcoming_shoots": sum(len(d["shoots"]) for d in days_output),
            "total_games": total_games_count,
            "unique_teams": len(total_unique_teams),
            "direct_matches": total_direct_matches,
            "maybe_matches": total_maybe_matches,
            "crm_records_scanned": len(crm_entries),
        },
        "days": days_output,
    }
    return report


def save_report_locally(report: dict) -> Path:
    from django.db import transaction
    from .models import GameOpportunitySnapshot

    DATA_FILE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    pending = None
    try:
        # Write privately to a sibling file, not on top of the last snapshot.
        with tempfile.NamedTemporaryFile(mode="w", dir=DATA_FILE.parent, prefix=".opportunities-", delete=False) as handle:
            pending = Path(handle.name)
            json.dump(report, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        with transaction.atomic():
            GameOpportunitySnapshot.objects.update_or_create(
                pk=1,
                defaults={"generated_at": report.get("generated_at", ""), "payload": report},
            )
            os.replace(pending, DATA_FILE)
    finally:
        if pending and pending.exists():
            pending.unlink()
    return DATA_FILE


def push_report_to_railway(report: dict, railway_url: str, secret: str) -> dict:
    """Push a locally generated report to the deployed Railway instance."""
    url = railway_url.rstrip("/") + "/api/opportunities/sync/"
    headers = {"Content-Type": "application/json"}
    if secret:
        headers["X-Sync-Secret"] = secret
    resp = requests.post(url, data=json.dumps(report), headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.json()


def load_saved_report() -> dict:
    db_report = None
    file_report = None
    try:
        from .models import GameOpportunitySnapshot

        snap = GameOpportunitySnapshot.objects.order_by("-updated_at").first()
        if snap and isinstance(snap.payload, dict) and snap.payload.get("days") is not None:
            db_report = snap.payload
    except Exception:
        pass
    if DATA_FILE.exists():
        try:
            loaded = json.loads(DATA_FILE.read_text())
            if isinstance(loaded, dict) and loaded.get("days") is not None:
                file_report = loaded
        except Exception:
            pass
    if db_report and file_report:
        if (db_report.get("generated_iso") or "") >= (file_report.get("generated_iso") or ""):
            return db_report
        return file_report
    if db_report:
        return db_report
    if file_report:
        return file_report
    return {
        "generated_at": "",
        "sites": TARGET_SITES,
        "summary": {
            "shoot_days": 0,
            "upcoming_shoots": 0,
            "total_games": 0,
            "unique_teams": 0,
            "direct_matches": 0,
            "maybe_matches": 0,
            "crm_records_scanned": 0,
        },
        "days": [],
    }
