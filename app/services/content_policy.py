"""Deterministic content policy: sensitivity, named entities, abstract lines,
statistics, ambiguous words, and avoid-term expansion.

This layer runs *regardless of which planner is used*, so safety never depends
on an LLM behaving. LLM flags are unioned with these rule flags, never replaced."""
import re

STOPWORDS = set("""
a an the and or but if then so of to in on at by for with from as is are was were be been being this that these those
it its it's we our you your they their he she his her i me my not no yes do does did have has had will would can could
should may might must just very really also more most less much many some any each every all into over under about
than there here what which who whom whose when where why how up down out off again further once only own same too
let lets let's s t don't isn't aren't wasn't weren't won't can't doesn't didn't one two three four five six seven eight
nine ten get got make makes made go goes going went thing things way ways like even still well now new because while
""".split())

SENSITIVE_TERMS = {
    "tragic": ["died", "death", "dead", "killed", "kill", "murder", "funeral", "victims", "victim", "tragedy", "tragic",
               "massacre", "suicide", "fatal", "fatalities", "casualties", "mourning", "mourn", "grief", "passed away",
               "शोक", "मृत्यु", "मौत", "இறப்பு", "மரணம்"],
    "sensitive": ["flood", "earthquake", "war", "attack", "crash", "disaster", "cancer", "disease", "illness", "injured",
                  "refugees", "violence", "poverty", "hunger", "famine", "wildfire", "hurricane", "cyclone", "abuse",
                  "pandemic", "layoffs", "accident", "hospital", "बाढ़", "भूकंप", "வெள்ளம்"],
}
# visuals that must not appear in sensitive / tragic scenes
SENSITIVE_BLOCK = ["blood", "gore", "corpse", "explosion", "fire", "burning", "weapon", "gun", "party", "celebration",
                   "dancing", "laughing", "smiling", "fun", "funny", "cheerful", "confetti", "fireworks", "crying"]
SENSITIVE_STYLE = "calm, respectful, muted colours, slow camera, no people in distress"

ABSTRACT_METAPHORS = {
    "trust": "two people shaking hands firmly in a calm office",
    "patience": "seedling growing slowly in soil time-lapse",
    "growth": "young plant sprouting in sunlight",
    "success": "person reaching a mountain summit at sunrise",
    "freedom": "bird flying over open landscape",
    "hope": "sunrise breaking through clouds over a city",
    "change": "seasons changing in a forest time-lapse",
    "time": "clock hands moving close-up",
    "teamwork": "team collaborating around a table with laptops",
    "focus": "camera lens adjusting focus on a single object",
    "habit": "person writing in a daily planner at a desk",
    "discipline": "runner training on an empty track at dawn",
    "values": "family sharing a quiet meal at home",
    "integrity": "person returning a lost wallet",
    "balance": "stacked stones balanced by a calm lake",
    "future": "city skyline at dawn with moving traffic",
    "innovation": "engineer assembling a prototype in a workshop",
    "risk": "climber on a rocky cliff edge",
    "saving": "coins being dropped into a glass jar",
    "savings": "coins being dropped into a glass jar",
    "wealth": "slowly growing stack of coins on a table",
    "knowledge": "student reading books in a quiet library",
    "learning": "student taking notes at a desk",
    "confidence": "speaker presenting to an audience on stage",
    "kindness": "volunteer handing food to a neighbour",
    "earned": "worker finishing a long task at a desk",
}

# word -> list of (context words, concrete visual). The first context hit wins; "" = default.
AMBIGUOUS = {
    "cloud": [({"computing", "data", "server", "servers", "storage", "software", "saas", "aws", "upload", "online"},
               "data center server racks with blinking lights"),
              ({"rain", "sky", "weather", "storm"}, "storm clouds moving across the sky"),
              (set(), "data center server racks with blinking lights")],
    "bank": [({"river", "water", "fishing", "stream", "shore"}, "grassy river bank with flowing water"),
             ({"blood", "donation"}, "blood donation centre"),
             (set(), "modern bank branch interior with customers at counters")],
    "mouse": [({"computer", "click", "laptop", "desk", "cursor", "pc", "keyboard", "screen"},
               "hand using a computer mouse on a desk"),
              ({"cat", "cheese", "pest", "field", "rodent", "trap"}, "small field mouse in grass"),
              (set(), "hand using a computer mouse on a desk")],
    "python": [({"snake", "reptile", "zoo", "jungle", "venom", "skin"}, "python snake coiled on a branch"),
               (set(), "programmer writing code on a laptop screen")],
    "value": [({"price", "market", "stock", "stocks", "money", "worth", "cost", "dollar", "rupees", "investment",
                "portfolio", "shares"}, "financial chart rising on a trading screen"),
              (set(), "family sharing a quiet meal at home")],
    "apple": [({"fruit", "eat", "orchard", "juice", "healthy", "tree"}, "fresh red apples in an orchard"),
              (set(), "person using a smartphone in a cafe")],
    "java": [({"coffee", "cup", "brew"}, "barista pouring coffee"), (set(), "programmer writing code on a laptop screen")],
    "crane": [({"bird", "lake", "wetland", "wings"}, "crane bird standing in a wetland"),
              (set(), "construction crane lifting steel beams")],
    "spring": [({"season", "flowers", "bloom", "april"}, "flowers blooming in spring"),
               (set(), "fresh water spring flowing over rocks")],
}

AVOID_SYNONYMS = {
    "face": ["face", "faces", "portrait", "selfie", "closeup", "close-up", "headshot"],
    "people": ["people", "person", "man", "woman", "men", "women", "girl", "boy", "child", "crowd", "face", "portrait"],
    "alcohol": ["alcohol", "beer", "wine", "whisky", "whiskey", "vodka", "cocktail", "liquor", "drinks", "bar", "pub",
                "champagne", "rum"],
    "smoking": ["smoking", "cigarette", "cigar", "smoke", "vape", "tobacco"],
    "violence": ["violence", "fight", "weapon", "gun", "blood", "war"],
    "animals": ["animal", "animals", "dog", "cat", "bird", "wildlife", "pet"],
    "meat": ["meat", "beef", "chicken", "pork", "steak", "butcher"],
    "religion": ["temple", "church", "mosque", "prayer", "religious"],
}

STAT_RE = re.compile(r"(\d+(\.\d+)?\s?%|\b\d{4}\b|\b\d+(\.\d+)?\s?(million|billion|crore|lakh|thousand|k)\b|\$\s?\d|₹\s?\d|\bpercent\b)", re.I)
NAME_STOP = {"I", "The", "A", "An", "In", "On", "At", "But", "And", "So", "This", "That", "It", "We", "You", "They",
             "Today", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday", "January",
             "February", "March", "April", "May", "June", "July", "August", "September", "October", "November",
             "December", "India", "Indian", "English", "Hindi", "Tamil", "AI", "CEO", "If", "When", "Our", "My",
             "Your", "Every", "Each", "As", "For", "With", "What", "How", "Why", "Then", "Now", "Here", "There"}
KNOWN_BRANDS = {"google", "apple", "microsoft", "amazon", "tesla", "nike", "adidas", "coca-cola", "pepsi", "samsung",
                "meta", "facebook", "instagram", "netflix", "tata", "reliance", "infosys", "flipkart", "zomato",
                "swiggy", "paytm", "uber", "ola", "starbucks", "mcdonald's", "openai", "nvidia", "intel", "sony"}


def tokens(text: str) -> list[str]:
    return re.findall(r"[\w'%-]+", text.lower())


def keywords(text: str, limit: int = 4) -> list[str]:
    seen, out = set(), []
    for t in tokens(text):
        t = t.strip("'-")
        if len(t) < 3 or t in STOPWORDS or t.isdigit() or t in seen:
            continue
        seen.add(t)
        out.append(t)
    out.sort(key=lambda w: -len(w))  # longer words tend to be more specific
    return out[:limit]


def sensitivity(text: str) -> str:
    low = text.lower()
    for level in ("tragic", "sensitive"):
        if any(re.search(rf"\b{re.escape(w)}\b", low) if w.isascii() else w in low for w in SENSITIVE_TERMS[level]):
            return level
    return "none"


def named_entities(text: str) -> list[str]:
    """Capitalised word runs that are not sentence-initial, plus known brands."""
    found: list[str] = []
    for sent in re.split(r"(?<=[.!?।])\s+", text):
        words = sent.split()
        run: list[str] = []
        for i, w in enumerate(words):
            clean = re.sub(r"[^\w'&.-]", "", w).strip(".")
            is_cap = clean[:1].isupper() and clean not in NAME_STOP and (i > 0 or len(words) == 1 or (i + 1 < len(words) and words[i + 1][:1].isupper()))
            if is_cap:
                run.append(clean)
            else:
                if run:
                    found.append(" ".join(run))
                run = []
        if run:
            found.append(" ".join(run))
    for t in tokens(text):
        if t in KNOWN_BRANDS and not any(t in f.lower() for f in found):
            found.append(t.title())
    return list(dict.fromkeys(found))


def strip_entities(query: str, entities: list[str]) -> str:
    for e in entities:
        for part in e.split():
            query = re.sub(rf"\b{re.escape(part)}\b", "", query, flags=re.I)
    return re.sub(r"\s+", " ", query).strip()


def has_statistic(text: str) -> bool:
    return bool(STAT_RE.search(text))


def abstract_concept(text: str) -> str | None:
    toks = tokens(text)
    for t in toks:
        if t in ABSTRACT_METAPHORS:
            return ABSTRACT_METAPHORS[t]
    return None


def disambiguate(text: str) -> tuple[str, str] | None:
    """Return (ambiguous word, concrete visual) when the text contains a double-meaning word."""
    toks = set(tokens(text))
    for word, senses in AMBIGUOUS.items():
        if word in toks or f"{word}s" in toks:
            for ctx, visual in senses:
                if not ctx or ctx & toks:
                    return word, visual
    return None


def expand_avoid(avoid: list[str]) -> list[str]:
    """Turn free-text avoid rules ("people's faces", "alcohol") into match terms."""
    out: set[str] = set()
    for a in avoid:
        low = a.lower()
        out.update(t for t in tokens(low) if t not in STOPWORDS and len(t) > 2)
        for key, syns in AVOID_SYNONYMS.items():
            if key in low:
                out.update(syns)
    return sorted(out)


def avoid_faces(avoid: list[str]) -> bool:
    return any(w in a.lower() for a in avoid for w in ("face", "people", "person", "human"))
