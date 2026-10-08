"""Static reference data for the ``small`` seed profile (hand-curated; no external datasets)."""

from __future__ import annotations

from typing import NamedTuple

FAMILY = "Outerwear"
SEASON = "AW26"
SIZES: tuple[str, ...] = ("XS", "S", "M", "L", "XL", "XXL")
BASE_SIZE_CURVE_BP: tuple[int, ...] = (600, 1600, 2800, 2600, 1600, 800)

MARKETS: tuple[tuple[str, str, str], ...] = (
    ("GB", "United Kingdom", "GBP"),
    ("IE", "Ireland", "EUR"),
    ("NL", "Netherlands", "EUR"),
    ("BE", "Belgium", "EUR"),
    ("FR", "France", "EUR"),
    ("DE", "Germany", "EUR"),
    ("AT", "Austria", "EUR"),
    ("PL", "Poland", "PLN"),
    ("CZ", "Czechia", "CZK"),
)

# code, name, country, city, lat, lon, inbound cap/wk, outbound cap/wk
DCS: tuple[tuple[str, str, str, str, float, float, int, int], ...] = (
    ("UK1", "Thurrock DC", "GB", "Thurrock", 51.4935, 0.3529, 60000, 55000),
    ("NL1", "Venlo DC", "NL", "Venlo", 51.3704, 6.1724, 55000, 50000),
    ("DE1", "Erfurt DC", "DE", "Erfurt", 50.9848, 11.0299, 50000, 48000),
    ("PL1", "Poznań DC", "PL", "Poznań", 52.4064, 16.9252, 30000, 28000),
)

# Which DC serves which market.
MARKET_DC: dict[str, str] = {
    "GB": "UK1",
    "IE": "UK1",
    "NL": "NL1",
    "BE": "NL1",
    "FR": "NL1",
    "DE": "DE1",
    "AT": "DE1",
    "CZ": "DE1",
    "PL": "PL1",
}


class StoreRef(NamedTuple):
    market: str
    name: str
    city: str
    lat: float
    lon: float
    grade: str  # A/B/C
    climate: str  # cold/mild/warm


STORES: tuple[StoreRef, ...] = (
    # GB / IE → UK1
    StoreRef("GB", "London Oxford Street", "London", 51.5152, -0.1418, "A", "mild"),
    StoreRef("GB", "London Stratford", "London", 51.5430, -0.0060, "A", "mild"),
    StoreRef("GB", "Birmingham Bull Ring", "Birmingham", 52.4778, -1.8936, "A", "mild"),
    StoreRef("GB", "Manchester Market Street", "Manchester", 53.4830, -2.2440, "A", "cold"),
    StoreRef("GB", "Glasgow Argyle Street", "Glasgow", 55.8590, -4.2540, "A", "cold"),
    StoreRef("GB", "Liverpool Church Street", "Liverpool", 53.4060, -2.9840, "B", "cold"),
    StoreRef("GB", "Leeds Briggate", "Leeds", 53.7980, -1.5420, "B", "cold"),
    StoreRef("GB", "Edinburgh Princes Street", "Edinburgh", 55.9520, -3.1960, "B", "cold"),
    StoreRef("GB", "Newcastle Northumberland Street", "Newcastle upon Tyne", 54.9760, -1.6130, "B", "cold"),
    StoreRef("GB", "Belfast Castle Street", "Belfast", 54.5990, -5.9330, "B", "cold"),
    StoreRef("GB", "Sheffield Fargate", "Sheffield", 53.3810, -1.4700, "C", "cold"),
    StoreRef("GB", "Bristol Broadmead", "Bristol", 51.4590, -2.5890, "B", "mild"),
    StoreRef("GB", "Cardiff Queen Street", "Cardiff", 51.4820, -3.1750, "C", "mild"),
    StoreRef("GB", "Nottingham Clumber Street", "Nottingham", 52.9550, -1.1460, "C", "mild"),
    StoreRef("GB", "Southampton Above Bar", "Southampton", 50.9070, -1.4040, "C", "mild"),
    StoreRef("IE", "Dublin Mary Street", "Dublin", 53.3490, -6.2650, "A", "mild"),
    StoreRef("IE", "Cork Patrick Street", "Cork", 51.8990, -8.4730, "B", "mild"),
    StoreRef("IE", "Limerick O'Connell Street", "Limerick", 52.6620, -8.6300, "C", "mild"),
    StoreRef("IE", "Galway Eyre Square", "Galway", 53.2740, -9.0490, "C", "mild"),
    # NL / BE / FR → NL1
    StoreRef("NL", "Amsterdam Kalverstraat", "Amsterdam", 52.3700, 4.8910, "A", "mild"),
    StoreRef("NL", "Rotterdam Lijnbaan", "Rotterdam", 51.9200, 4.4760, "A", "mild"),
    StoreRef("NL", "Den Haag Grote Marktstraat", "The Hague", 52.0770, 4.3110, "B", "mild"),
    StoreRef("NL", "Utrecht Hoog Catharijne", "Utrecht", 52.0900, 5.1110, "B", "mild"),
    StoreRef("NL", "Eindhoven Demer", "Eindhoven", 51.4400, 5.4780, "C", "mild"),
    StoreRef("NL", "Groningen Herestraat", "Groningen", 53.2160, 6.5680, "C", "mild"),
    StoreRef("BE", "Brussels Rue Neuve", "Brussels", 50.8520, 4.3560, "A", "mild"),
    StoreRef("BE", "Antwerp Meir", "Antwerp", 51.2180, 4.4090, "B", "mild"),
    StoreRef("BE", "Ghent Veldstraat", "Ghent", 51.0510, 3.7230, "C", "mild"),
    StoreRef("BE", "Liège Pont d'Avroy", "Liège", 50.6400, 5.5700, "C", "mild"),
    StoreRef("FR", "Paris Rivoli", "Paris", 48.8600, 2.3410, "A", "mild"),
    StoreRef("FR", "Lille Rue Neuve", "Lille", 50.6370, 3.0630, "B", "mild"),
    StoreRef("FR", "Strasbourg Place Kléber", "Strasbourg", 48.5830, 7.7460, "C", "mild"),
    StoreRef("FR", "Lyon Part-Dieu", "Lyon", 45.7610, 4.8570, "B", "warm"),
    StoreRef("FR", "Marseille Terrasses du Port", "Marseille", 43.3050, 5.3660, "B", "warm"),
    StoreRef("FR", "Bordeaux Sainte-Catherine", "Bordeaux", 44.8380, -0.5740, "B", "warm"),
    StoreRef("FR", "Toulouse Rue d'Alsace-Lorraine", "Toulouse", 43.6040, 1.4440, "C", "warm"),
    StoreRef("FR", "Nice Jean Médecin", "Nice", 43.7020, 7.2660, "C", "warm"),
    # DE / AT / CZ → DE1
    StoreRef("DE", "Berlin Alexanderplatz", "Berlin", 52.5210, 13.4130, "A", "cold"),
    StoreRef("DE", "Hamburg Mönckebergstraße", "Hamburg", 53.5510, 9.9970, "A", "cold"),
    StoreRef("DE", "München Neuhauser Straße", "Munich", 48.1390, 11.5660, "A", "cold"),
    StoreRef("DE", "Leipzig Petersstraße", "Leipzig", 51.3380, 12.3740, "B", "cold"),
    StoreRef("DE", "Dresden Prager Straße", "Dresden", 51.0460, 13.7350, "C", "cold"),
    StoreRef("DE", "Nürnberg Breite Gasse", "Nuremberg", 49.4500, 11.0740, "C", "cold"),
    StoreRef("DE", "Hannover Georgstraße", "Hanover", 52.3740, 9.7370, "C", "cold"),
    StoreRef("DE", "Erfurt Anger", "Erfurt", 50.9760, 11.0330, "C", "cold"),
    StoreRef("DE", "Köln Schildergasse", "Cologne", 50.9360, 6.9530, "B", "mild"),
    StoreRef("DE", "Düsseldorf Schadowstraße", "Düsseldorf", 51.2270, 6.7830, "B", "mild"),
    StoreRef("DE", "Frankfurt Zeil", "Frankfurt am Main", 50.1140, 8.6830, "B", "mild"),
    StoreRef("DE", "Stuttgart Königstraße", "Stuttgart", 48.7780, 9.1790, "C", "mild"),
    StoreRef("AT", "Wien Mariahilfer Straße", "Vienna", 48.1990, 16.3520, "A", "cold"),
    StoreRef("AT", "Graz Annenstraße", "Graz", 47.0710, 15.4300, "B", "cold"),
    StoreRef("AT", "Linz Landstraße", "Linz", 48.3030, 14.2900, "C", "cold"),
    StoreRef("CZ", "Praha Václavské náměstí", "Prague", 50.0810, 14.4270, "A", "cold"),
    StoreRef("CZ", "Brno Galerie Vaňkovka", "Brno", 49.1890, 16.6150, "B", "cold"),
    # PL → PL1
    StoreRef("PL", "Warszawa Złote Tarasy", "Warsaw", 52.2300, 21.0030, "A", "cold"),
    StoreRef("PL", "Kraków Galeria Krakowska", "Kraków", 50.0670, 19.9450, "A", "cold"),
    StoreRef("PL", "Poznań Stary Browar", "Poznań", 52.4030, 16.9270, "B", "cold"),
    StoreRef("PL", "Wrocław Pasaż Grunwaldzki", "Wrocław", 51.1120, 17.0600, "B", "cold"),
    StoreRef("PL", "Gdańsk Galeria Bałtycka", "Gdańsk", 54.3830, 18.5990, "B", "cold"),
    StoreRef("PL", "Łódź Manufaktura", "Łódź", 51.7790, 19.4480, "C", "cold"),
    StoreRef("PL", "Katowice Galeria Katowicka", "Katowice", 50.2590, 19.0170, "C", "cold"),
    StoreRef("PL", "Szczecin Galaxy", "Szczecin", 53.4310, 14.5370, "C", "cold"),
)

# code, name, country, kind
SUPPLIERS: tuple[tuple[str, str, str, str], ...] = (
    ("TRIM-BD-01", "Meghna Trims Ltd", "BD", "trim"),
    ("TRIM-KH-01", "Tonle Sap Trims Co.", "KH", "trim"),
    ("FAB-CN-01", "Ningbo Shell Fabrics Co.", "CN", "fabric"),
    ("CMT-BD-01", "Dhaka Outerwear Manufacturing Ltd", "BD", "cmt"),
    ("CMT-KH-01", "Mekong Garment Industries", "KH", "cmt"),
)

# code, name, country, supplier
FACTORIES: tuple[tuple[str, str, str, str], ...] = (
    ("F-BD-01", "Gazipur Outerwear Unit 1", "BD", "CMT-BD-01"),
    ("F-BD-02", "Narayanganj Outerwear Unit 2", "BD", "CMT-BD-01"),
    ("F-KH-01", "Phnom Penh Garment Plant", "KH", "CMT-KH-01"),
)

# code, name, kind, supplier
COMPONENTS: tuple[tuple[str, str, str, str], ...] = (
    ("CMP-WAD-01", "Polyester wadding 120 gsm", "wadding", "TRIM-BD-01"),
    ("CMP-ZIP-01", "Two-way metal zip 70 cm", "zip", "TRIM-BD-01"),
    ("CMP-WAD-02", "Recycled polyester wadding 120 gsm", "wadding", "TRIM-KH-01"),
    ("CMP-ZIP-02", "Two-way coil zip 70 cm", "zip", "TRIM-KH-01"),
    ("CMP-SHL-01", "Ripstop nylon shell fabric", "shell_fabric", "FAB-CN-01"),
    ("CMP-LIN-01", "Taffeta lining", "lining", "FAB-CN-01"),
)

# component, factory (None = every factory), qty per unit
BOM_TEMPLATE: tuple[tuple[str, str | None, int], ...] = (
    ("CMP-SHL-01", None, 2),
    ("CMP-LIN-01", None, 1),
    ("CMP-WAD-01", "F-BD-01", 1),
    ("CMP-ZIP-01", "F-BD-01", 1),
    ("CMP-WAD-01", "F-BD-02", 1),
    ("CMP-ZIP-01", "F-BD-02", 1),
    ("CMP-WAD-02", "F-KH-01", 1),
    ("CMP-ZIP-02", "F-KH-01", 1),
)

# code, name, colour, retail cents, cost cents, popularity weight
STYLES: tuple[tuple[str, str, str, int, int, float], ...] = (
    ("PUF-W-BLK", "Women's Longline Puffer Jacket", "Black", 3500, 1150, 1.30),
    ("PUF-W-CRM", "Women's Longline Puffer Jacket", "Cream", 3500, 1150, 0.80),
    ("PUF-W-KHK", "Women's Cropped Puffer Jacket", "Khaki", 2800, 900, 0.90),
    ("PUF-M-BLK", "Men's Hooded Puffer Jacket", "Black", 3200, 1050, 1.20),
    ("PUF-M-NVY", "Men's Hooded Puffer Jacket", "Navy", 3200, 1050, 1.00),
)

# Forecast horizon and seasonality (Black Friday 2026-11-27 falls in W48).
FORECAST_WEEKS: tuple[str, ...] = tuple(f"2026-W{w}" for w in range(41, 50))
WEEK_UPLIFT: dict[str, float] = {
    "2026-W41": 0.90,
    "2026-W42": 0.95,
    "2026-W43": 1.00,
    "2026-W44": 1.05,
    "2026-W45": 1.60,
    "2026-W46": 1.80,
    "2026-W47": 1.90,
    "2026-W48": 2.10,
    "2026-W49": 1.20,
}
CLIMATE_FACTOR: dict[str, float] = {"cold": 1.15, "mild": 1.00, "warm": 0.75}
BASE_UNITS_PER_INDEX = 12.0
GRADE_INDEX_RANGE: dict[str, tuple[float, float]] = {"A": (1.6, 2.2), "B": (1.0, 1.4), "C": (0.6, 0.9)}

# DCs that depend on Bangladesh supply for peak (shortage story) vs. surplus DCs.
SHORT_DCS: tuple[str, ...] = ("UK1", "NL1")
SURPLUS_RATIO: dict[str, float] = {"DE1": 0.30, "PL1": 0.25}
# The surplus DCs are tight in these sizes (the Cambodia PO was cut to the core sizes), so a
# DC-to-DC rebalance recovers only part of the units a Bangladesh delay puts at risk.
TIGHT_SIZES: tuple[str, ...] = ("XS", "XL", "XXL")
TIGHT_SURPLUS_RATIO = 0.04
# Floors for the end-of-horizon buffers (on top of the size-split rounding slack).
SURPLUS_MIN_UNITS = 8

# Short-DC supply plan (see generator docstring). Buffers are fractions of that SKU's
# weekly DC demand:
#   PRE_A_BUFFER   stock left after W44 if wave A (ETA W44) is late        (x W41 demand)
#   W45_COVER      extra W45 cover from wave A for the W45-covered styles  (x W45 demand)
#   W46_CARRY      baseline stock carried into W46; a 7-day BD delay loses
#                  about (1 - W46_CARRY) x W46 demand per short DC x SKU   (x W46 demand)
#   END_BUFFER     stock left after W49                                     (x W49 demand)
STORY_PRE_A_BUFFER = 0.30
STORY_W45_COVER = 0.10
STORY_W46_CARRY = 0.50
STORY_END_BUFFER = 0.30
STORY_END_MIN_UNITS = 8
# Styles whose wave A also covers W45: when BD is 7 days late they first breach in W46,
# the others in W45 (so the demo shows more than one first-breach week).
W45_COVERED_STYLES: tuple[str, ...] = ("PUF-W-CRM", "PUF-M-NVY")
