"""Pop announcements. One is picked at random each time a moon pops."""
from __future__ import annotations

import random

HEADLINES = [
    "💥 MOON POPPED! 💥",
    "🪅 PIÑATA TIME! 🪅",
    "⛏️ ROCKS ARE OUT! ⛏️",
    "🌕💥 THE MOON HAS BEEN CRACKED 💥🌕",
    "🚨 FRESH ORE ON GRID 🚨",
]

BODIES = [
    "**{moon}** just cracked open like a piñata. {collector} wants the moon tax and the rocks won't mine themselves. 💸",
    "**{moon}** went boom. Somewhere, {collector} is already sharpening the moon tax spreadsheet. 📊",
    "The laser did its thing and **{moon}** is now a very expensive pile of gravel. Let's go get it before it gets bored and despawns.",
    "**{moon}** has been fractured. The ore is floating there, waiting, judging you for not undocking yet. 👀",
    "Breaking news: **{moon}** is open for business. The moon tax for {collector} isn't going to pay itself. 💰",
    "**{moon}** popped! Your Hulk has been asking about this for weeks. Don't let it down. 🥺",
    "Rocks detected at **{moon}**. Exhumers to the front, Orcas to the middle, snacks everywhere. 🍿",
]

CALLS = [
    "**Who's joining the mining posse? Bring a Hulk, a Hammer, and snacks.** ⛏️🔨",
    "**X up in fleet chat — barges, exhumers, and at least one guy who brings the boosts.** 🚀",
    "**Grab your barges and form up. Strip it like it owes us ISK!** 🔥⛏️",
    "**Undock, align, mine. It's that simple. (It's never that simple.)** 😅",
    "**Fleet up! Last one on grid buys the next round of fuel blocks.** ⛽",
]


def build_pop(moon: str, structure: str, system: str, ore_names: list[str],
              total: str | None, collector: str = "The tax collector",
              rng: random.Random | None = None) -> tuple[str, dict]:
    """Return (headline, embed dict) for a pop announcement."""
    r = rng or random
    lines = [r.choice(BODIES).format(moon=moon, collector=collector), ""]
    if ore_names:
        lines.append(f"🪨 **Ore:** {' · '.join(ore_names)}" + (f" (≈{total})" if total else ""))
    lines.append(f"📍 **Location:** {system} — {structure}")
    lines += ["", r.choice(CALLS), "React with ⛏️ if you're in!"]
    embed = {
        "title": r.choice(HEADLINES),
        "description": "\n".join(lines),
        "color": 0xF97316,
    }
    return embed["title"], embed
