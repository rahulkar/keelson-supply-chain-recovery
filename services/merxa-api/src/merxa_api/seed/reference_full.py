"""Static reference data for the ``full`` seed profile (docs/contracts/22-merxa-m2.md §2).

Store locations come from the bundled city table ``seed/data/cities.csv`` (see README ›
Seed › City data); everything else is hand-curated here.
"""

from __future__ import annotations

from typing import NamedTuple

FAMILY_OUTERWEAR = "Outerwear"
FAMILY_KNIT = "Knit"
SEASON = "AW26"

MARKETS: tuple[tuple[str, str, str], ...] = (
    ("GB", "United Kingdom", "GBP"),
    ("IE", "Ireland", "EUR"),
    ("NL", "Netherlands", "EUR"),
    ("BE", "Belgium", "EUR"),
    ("FR", "France", "EUR"),
    ("ES", "Spain", "EUR"),
    ("PT", "Portugal", "EUR"),
    ("DE", "Germany", "EUR"),
    ("AT", "Austria", "EUR"),
    ("IT", "Italy", "EUR"),
    ("SI", "Slovenia", "EUR"),
    ("PL", "Poland", "PLN"),
    ("CZ", "Czechia", "CZK"),
    ("SK", "Slovakia", "EUR"),
    ("RO", "Romania", "RON"),
    ("HU", "Hungary", "HUF"),
)

STORE_COUNTS: dict[str, int] = {
    "GB": 160,
    "ES": 70,
    "DE": 45,
    "IE": 30,
    "FR": 25,
    "NL": 20,
    "IT": 20,
    "PL": 20,
    "PT": 15,
    "BE": 10,
    "CZ": 8,
    "RO": 8,
    "AT": 6,
    "HU": 6,
    "SK": 4,
    "SI": 4,
}
assert sum(STORE_COUNTS.values()) == 451

MARKET_DC: dict[str, str] = {
    "GB": "UK1",
    "IE": "UK1",
    "NL": "NL1",
    "BE": "NL1",
    "FR": "NL1",
    "ES": "NL1",
    "PT": "NL1",
    "DE": "DE1",
    "AT": "DE1",
    "IT": "DE1",
    "SI": "DE1",
    "PL": "PL1",
    "CZ": "PL1",
    "SK": "PL1",
    "RO": "PL1",
    "HU": "PL1",
}

# code, name, country, city, lat, lon, inbound cap/wk, outbound cap/wk
DCS: tuple[tuple[str, str, str, str, float, float, int, int], ...] = (
    ("UK1", "Thurrock DC", "GB", "Thurrock", 51.4935, 0.3529, 120000, 110000),
    ("NL1", "Venlo DC", "NL", "Venlo", 51.3704, 6.1724, 110000, 100000),
    ("DE1", "Erfurt DC", "DE", "Erfurt", 50.9848, 11.0299, 90000, 85000),
    ("PL1", "Poznań DC", "PL", "Poznań", 52.4064, 16.9252, 60000, 55000),
)

# Store-placement knobs: population weight exponent and jitter (≈ 2 km).
POPULATION_EXPONENT = 0.6
JITTER_DEG = 0.012
STORE_SUFFIXES: tuple[str, ...] = (
    "Centre",
    "High Street",
    "Retail Park",
    "Shopping Centre",
    "Station",
    "North",
    "South",
    "East",
    "West",
    "Riverside",
    "Old Town",
    "Gateway",
)
# Climate: cold in these markets (north of 45°N) and anywhere north of 53.5°N or in eastern
# Germany; warm south of 44.5°N; mild otherwise.
COLD_MARKETS: frozenset[str] = frozenset({"PL", "CZ", "SK", "HU", "RO", "AT"})
COLD_LAT = 53.5
WARM_LAT = 44.5
DE_EAST_LON = 10.5
# Grade probabilities (A, B, C) by city population tier.
GRADE_P: tuple[tuple[int, tuple[float, float, float]], ...] = (
    (500_000, (0.40, 0.45, 0.15)),
    (150_000, (0.15, 0.50, 0.35)),
    (0, (0.05, 0.35, 0.60)),
)

# code, name, country, kind
SUPPLIERS: tuple[tuple[str, str, str, str], ...] = (
    ("TRIM-BD-01", "Meghna Trims Ltd", "BD", "trim"),
    ("TRIM-KH-01", "Tonle Sap Trims Co.", "KH", "trim"),
    ("TRIM-TR-01", "Bursa Trim Sanayi A.S.", "TR", "trim"),
    ("FAB-CN-01", "Ningbo Shell Fabrics Co.", "CN", "fabric"),
    ("CMT-BD-01", "Dhaka Outerwear Manufacturing Ltd", "BD", "cmt"),
    ("CMT-KH-01", "Mekong Garment Industries", "KH", "cmt"),
    ("SUP-VN-01", "Saigon Knit Co.", "VN", "cmt"),
    ("CMT-TR-01", "Anatolia Tekstil A.S.", "TR", "cmt"),
    ("CMT-MA-01", "Tanger Confection SARL", "MA", "cmt"),
)

# code, name, country, supplier
FACTORIES: tuple[tuple[str, str, str, str], ...] = (
    ("F-BD-01", "Gazipur Outerwear Unit 1", "BD", "CMT-BD-01"),
    ("F-BD-02", "Narayanganj Outerwear Unit 2", "BD", "CMT-BD-01"),
    ("F-KH-01", "Phnom Penh Garment Plant", "KH", "CMT-KH-01"),
    ("F-VN-01", "Saigon Knit Thu Duc Plant", "VN", "SUP-VN-01"),
    ("F-TR-01", "Istanbul Esenyurt Plant", "TR", "CMT-TR-01"),
    ("F-MA-01", "Tanger Free Zone Plant", "MA", "CMT-MA-01"),
)

# code, name, kind, supplier
COMPONENTS: tuple[tuple[str, str, str, str], ...] = (
    ("CMP-WAD-01", "Polyester wadding 120 gsm", "wadding", "TRIM-BD-01"),
    ("CMP-ZIP-01", "Two-way metal zip 70 cm", "zip", "TRIM-BD-01"),
    ("CMP-WAD-02", "Recycled polyester wadding 120 gsm", "wadding", "TRIM-KH-01"),
    ("CMP-ZIP-02", "Two-way coil zip 70 cm", "zip", "TRIM-KH-01"),
    ("CMP-WAD-03", "Polyester wadding 100 gsm", "wadding", "TRIM-TR-01"),
    ("CMP-ZIP-03", "Coil zip 65 cm", "zip", "TRIM-TR-01"),
    ("CMP-SHL-01", "Ripstop nylon shell fabric", "shell_fabric", "FAB-CN-01"),
    ("CMP-LIN-01", "Taffeta lining", "lining", "FAB-CN-01"),
    ("CMP-YRN-01", "Acrylic-wool blend yarn", "yarn", "SUP-VN-01"),
    ("CMP-FLC-01", "Polyester teddy fleece", "fleece_fabric", "SUP-VN-01"),
    ("CMP-YRN-02", "Acrylic yarn (near-shore)", "yarn", "CMT-TR-01"),
    ("CMP-LBL-01", "Woven care label", "label", "FAB-CN-01"),
)

# component, factory (None = every factory making the style), qty per unit
BOM_OUTERWEAR: tuple[tuple[str, str | None, int], ...] = (
    ("CMP-SHL-01", None, 2),
    ("CMP-LIN-01", None, 1),
    ("CMP-LBL-01", None, 1),
    ("CMP-WAD-01", "F-BD-01", 1),
    ("CMP-ZIP-01", "F-BD-01", 1),
    ("CMP-WAD-01", "F-BD-02", 1),
    ("CMP-ZIP-01", "F-BD-02", 1),
    ("CMP-WAD-02", "F-KH-01", 1),
    ("CMP-ZIP-02", "F-KH-01", 1),
    ("CMP-WAD-03", "F-TR-01", 1),
    ("CMP-ZIP-03", "F-TR-01", 1),
    ("CMP-WAD-03", "F-MA-01", 1),
    ("CMP-ZIP-03", "F-MA-01", 1),
)
BOM_KNIT: tuple[tuple[str, str | None, int], ...] = (
    ("CMP-LBL-01", None, 1),
    ("CMP-YRN-01", "F-VN-01", 1),
    ("CMP-FLC-01", "F-VN-01", 1),
    ("CMP-YRN-02", "F-TR-01", 1),
    ("CMP-YRN-02", "F-MA-01", 1),
)


class StyleRef(NamedTuple):
    code: str
    name: str
    colour: str
    retail_cents: int
    cost_cents: int
    popularity: float
    family: str


_O, _K = FAMILY_OUTERWEAR, FAMILY_KNIT
STYLES: tuple[StyleRef, ...] = (
    # Outerwear (puffer / parka / gilet) — the first five are the `small` styles.
    StyleRef("PUF-W-BLK", "Women's Longline Puffer Jacket", "Black", 3500, 1150, 1.30, _O),
    StyleRef("PUF-W-CRM", "Women's Longline Puffer Jacket", "Cream", 3500, 1150, 0.80, _O),
    StyleRef("PUF-W-KHK", "Women's Cropped Puffer Jacket", "Khaki", 2800, 900, 0.90, _O),
    StyleRef("PUF-M-BLK", "Men's Hooded Puffer Jacket", "Black", 3200, 1050, 1.20, _O),
    StyleRef("PUF-M-NVY", "Men's Hooded Puffer Jacket", "Navy", 3200, 1050, 1.00, _O),
    StyleRef("PUF-W-SGE", "Women's Cropped Puffer Jacket", "Sage", 2800, 900, 0.70, _O),
    StyleRef("PUF-W-PNK", "Women's Longline Puffer Jacket", "Blush", 3500, 1150, 0.60, _O),
    StyleRef("PUF-M-OLV", "Men's Hooded Puffer Jacket", "Olive", 3200, 1050, 0.80, _O),
    StyleRef("PRK-W-BLK", "Women's Faux-Fur Hood Parka", "Black", 4000, 1350, 1.00, _O),
    StyleRef("PRK-W-CML", "Women's Faux-Fur Hood Parka", "Camel", 4000, 1350, 0.70, _O),
    StyleRef("PRK-M-BLK", "Men's Borg-Lined Parka", "Black", 4200, 1400, 0.90, _O),
    StyleRef("PRK-M-KHK", "Men's Borg-Lined Parka", "Khaki", 4200, 1400, 0.80, _O),
    StyleRef("PRK-M-NVY", "Men's Borg-Lined Parka", "Navy", 4200, 1400, 0.60, _O),
    StyleRef("GIL-M-BLK", "Men's Padded Gilet", "Black", 1800, 600, 0.70, _O),
    StyleRef("GIL-W-NVY", "Women's Padded Gilet", "Navy", 1800, 600, 0.60, _O),
    # Knit (fleece / jumpers / cardigans)
    StyleRef("FLC-W-GRY", "Women's Teddy Fleece Jacket", "Grey", 1500, 450, 1.20, _K),
    StyleRef("FLC-W-CRM", "Women's Teddy Fleece Jacket", "Cream", 1500, 450, 1.00, _K),
    StyleRef("FLC-W-PNK", "Women's Teddy Fleece Jacket", "Pink", 1500, 450, 0.70, _K),
    StyleRef("FLC-M-NVY", "Men's Half-Zip Fleece", "Navy", 1400, 420, 1.10, _K),
    StyleRef("FLC-M-BLK", "Men's Half-Zip Fleece", "Black", 1400, 420, 1.00, _K),
    StyleRef("FLC-M-GRN", "Men's Half-Zip Fleece", "Forest", 1400, 420, 0.60, _K),
    StyleRef("KNT-W-OAT", "Women's Cable Knit Jumper", "Oatmeal", 1600, 500, 1.10, _K),
    StyleRef("KNT-W-BRG", "Women's Cable Knit Jumper", "Burgundy", 1600, 500, 0.80, _K),
    StyleRef("KNT-W-BLK", "Women's Cable Knit Jumper", "Black", 1600, 500, 0.90, _K),
    StyleRef("KNT-M-NVY", "Men's Crew Neck Jumper", "Navy", 1500, 460, 1.00, _K),
    StyleRef("KNT-M-GRY", "Men's Crew Neck Jumper", "Grey", 1500, 460, 0.90, _K),
    StyleRef("KNT-M-ECR", "Men's Crew Neck Jumper", "Ecru", 1500, 460, 0.60, _K),
    StyleRef("CRD-W-CML", "Women's Chunky Cardigan", "Camel", 1800, 560, 0.70, _K),
    StyleRef("CRD-W-GRN", "Women's Chunky Cardigan", "Sage", 1800, 560, 0.50, _K),
    StyleRef("HDY-U-BLK", "Unisex Fleece Hoodie", "Black", 1300, 380, 1.00, _K),
)
assert len(STYLES) == 30 and len({s.code for s in STYLES}) == 30

BASE_SIZE_CURVE_BP: dict[str, tuple[int, ...]] = {
    FAMILY_OUTERWEAR: (600, 1600, 2800, 2600, 1600, 800),
    FAMILY_KNIT: (700, 1700, 2700, 2500, 1600, 800),
}
CLIMATE_FACTOR: dict[str, dict[str, float]] = {
    FAMILY_OUTERWEAR: {"cold": 1.15, "mild": 1.00, "warm": 0.75},
    FAMILY_KNIT: {"cold": 1.05, "mild": 1.00, "warm": 0.90},
}
BASE_UNITS_PER_INDEX = 4.0
# Planned allocations are generated for these weeks only (W41 released, W42 planned) —
# 451 stores × 180 SKUs × 9 weeks would be ~0.7 M rows.
ALLOCATION_WEEKS: tuple[str, ...] = ("2026-W41", "2026-W42")

# Same story knobs as `small` for outerwear at UK1/NL1 (Bangladesh supply) and DE1/PL1 surplus.
W45_COVERED_STYLES: tuple[str, ...] = ("PUF-W-CRM", "PUF-M-NVY", "PRK-W-CML", "PRK-M-NVY", "GIL-W-NVY")
# Knit at UK1/NL1 is fully stocked (+ this surplus); knit at DE1/PL1 depends on the
# Vietnamese POs (ETA W46 / W47 via Ho Chi Minh City).
KNIT_SURPLUS_RATIO = 0.12
VN_PRE_BUFFER = 0.30  # stock left after W45 (x W45 demand)
# Stock carried from wave 1 into W47 (x W47 demand). M4: 0.5 → 0.2 so that wave 1 alone
# cannot carry the Black-Friday week when both waves come round the Cape (+12 days): the
# 21-day Suez closure then runs DE1/PL1 short of knit in W46, W47 **and** W48.
VN_W47_CARRY = 0.20
VN_CLOSURE_DAYS = 10
# Act 2 what-if (docs/contracts/90 §8): Suez closed for 21 days from the sim "today".
SUEZ_CHOKEPOINT = "SUEZ"
SUEZ_CLOSURE_DAYS = 21
