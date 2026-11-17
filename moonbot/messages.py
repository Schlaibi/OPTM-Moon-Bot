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
    "The laser did its thing and **{moon}** is now a very expensive pile of gravel. It won't stay there forever.",
    "**{moon}** has been fractured. The ore is floating there, waiting for someone to organise a fleet. 👀",
    "Breaking news: **{moon}** is open for business. The moon tax for {collector} isn't going to pay itself. 💰",
    "**{moon}** popped! Your Hulk has been asking about this for weeks. Don't let it down. 🥺",
    "Rocks detected at **{moon}**. All we're missing is a fleet. And an FC. And boosts. Details. 🍿",
]

CALLS = [
    "**Who's organising the mining posse? Someone grab the FC hat — Hulks, Hammers and snacks will follow.** ⛏️🔨",
    "**We need an FC and someone with boosts. Volunteers get eternal glory (and first pick of the asteroids).** 🚀",
    "**Who's up for a fleet? Speak up, pick a time, and we'll strip it like it owes us ISK.** 🔥⛏️",
    "**Step 1: someone forms a fleet. Step 2: everyone else joins. Step 1 is the hard part.** 😅",
    "**Who can FC this one? Drop a time below and let's get a fleet together.** 📅",
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
    lines += ["", r.choice(CALLS),
              "React with ⛏️ if you'd join a fleet — and say so if you can FC or bring boosts!"]
    embed = {
        "title": r.choice(HEADLINES),
        "description": "\n".join(lines),
        "color": 0xF97316,
    }
    return embed["title"], embed
