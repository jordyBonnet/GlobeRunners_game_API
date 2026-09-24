"""Card-art URLs from the GitHub Releases of `jordyBonnet/GlobeRunners_images`.

Self-contained mirror of the deckbuilding app's `lib/github_assets.py`
(`GlobeRunners_deckbuild_app`) so this repo has no cross-repo dependency.

Used by `game_ui/app.py` when `LOCAL_TEST` is **False**: `GET /art/<card_id>.png`
redirects to the matching GitHub Release asset instead of being served from the
local art folder (this is how the deckbuilding app serves card images on
Hugging Face).

Note: only the **six main factions** (Dwa/Dem/Twi/Mia/Orc/Mum) are published as
release assets. Support-faction art (`Eng_*`/`Doc_*`/`Mag_*`) is NOT in any
release — the app redirects those to `placeholder.svg` instead.
"""

# Base URL for card images on GitHub Releases
GITHUB_RELEASE_BASE = "https://github.com/jordyBonnet/GlobeRunners_images/releases/download"

# Map faction abbreviations to their release tags
FACTION_TAGS = {
    "Dwa": "v0.6-dwarves",
    "Dem": "v0.6-demons",
    "Twi": "v0.6-twigs",
    "Mia": "v0.6-miaous",
    "Orc": "v0.6-orcs",
    "Mum": "v0.6-mummies",
}

# Split point for Twigs faction (cards before this go to part2)
TWIGS_SPLIT_POINT = "Twi11_4beb34"
# Split point for Orcs faction
ORCS_SPLIT_POINT = "Orc32_403c92"


def get_faction_from_card_id(card_id):
    """Extract the release tag for a card_id (format: Dwa22_20e631)."""
    # Get first 3 characters (faction abbreviation)
    faction_abbr = card_id[:3]

    # Special handling for Twigs split across two releases
    if faction_abbr == "Twi":
        # Compare alphabetically - if card_id comes before the split point, use part2
        if card_id < TWIGS_SPLIT_POINT:
            return "v0.6-twigs-part2"

    # Special handling for Orcs split across two releases
    if faction_abbr == "Orc":
        # Compare alphabetically - if card_id comes before the split point, use part2
        if card_id < ORCS_SPLIT_POINT:
            return "v0.6-orcs-part2"

    # Special handling for Miaous split across 4 releases
    if faction_abbr == "Mia":
        # Get the 4th character (index 3) which is 1, 2, 3, or 4
        if len(card_id) > 3:
            part = card_id[3]
            if part in ['1', '2', '3', '4']:
                return f"v0.6-miaous-{part}"

    return FACTION_TAGS.get(faction_abbr, "v0.6-dwarves")  # Default to dwarves


def get_image_url(card_id):
    """Get direct URL to a card image on GitHub Releases (card_id without .png)."""
    tag = get_faction_from_card_id(card_id)
    return f"{GITHUB_RELEASE_BASE}/{tag}/{card_id}.png"


def has_github_art(filename):
    """True if the file belongs to one of the six main factions — i.e. has a
    GitHub Release asset. Support-faction art (Eng_/Doc_/Mag_) is not published
    as release assets."""
    return filename[:3] in FACTION_TAGS
