"""Deterministic mock stock library: real, moving video clips synthesised with
ffmpeg (owned by us, so licence-clean), with realistic metadata. Includes
deliberately bad assets (watermarked, no licence, editorial-only, corrupt,
too short, faces, alcohol) so the gates are exercised in tests."""
import hashlib
from pathlib import Path

from app.adapters.footage.base import apply_download_fault, apply_search_fault
from app.schemas.footage import Attribution, FootageCandidate, Licence
from app.utils.ffmpeg import run_ffmpeg

LICENCE = Licence(name="Mock Owned Footage Licence", url="https://example.invalid/mock-licence",
                  commercial_use_allowed=True, attribution_required=False, evidence_status="provider_policy_v1")

# (asset_id, title, tags, duration, special)
CATALOG = [
    ("office-laptop", "Person typing on laptop in bright office", "office,laptop,work,typing,business", 12, ""),
    ("server-racks", "Data center server racks with blinking lights", "data center,server,cloud computing,technology,network", 14, ""),
    ("mouse-hand", "Hand using a computer mouse on desk", "computer mouse,hand,desk,click,computer", 10, ""),
    ("code-screen", "Programmer writing code on laptop screen", "programmer,code,python,software,developer,laptop", 12, ""),
    ("river-bank", "Grassy river bank with flowing water", "river bank,river,water,nature,grass", 15, ""),
    ("bank-branch", "Modern bank branch interior with customers", "bank,finance,branch,counter,customers", 11, ""),
    ("coin-jar", "Coins being dropped into a glass jar", "coins,saving,savings,jar,money", 9, ""),
    ("stock-chart", "Financial chart rising on trading screen", "chart,finance,stock market,trading,graph,value", 12, ""),
    ("business-charts", "Analyst reviewing business charts and reports", "business,charts,reports,analyst,laptop,data", 13, ""),
    ("family-meal", "Family sharing a quiet meal at home", "family,meal,home,dinner,together,values", 12, ""),
    ("handshake", "Two people shaking hands firmly in office", "handshake,trust,agreement,business,partnership", 10, ""),
    ("seedling", "Seedling growing in soil time-lapse", "seedling,plant,growth,patience,nature,growing", 14, ""),
    ("skyline-dawn", "City skyline at dawn with traffic", "city,skyline,dawn,traffic,future,urban", 15, ""),
    ("candles", "Candles glowing softly in a dark quiet room", "candles,memorial,quiet,calm,respect,mourning", 12, ""),
    ("quiet-street", "Quiet empty street in soft morning light", "street,quiet,morning,empty,calm", 13, ""),
    ("forest", "Sunlight through forest trees", "forest,trees,nature,sunlight,calm", 15, ""),
    ("ocean", "Ocean waves rolling onto beach", "ocean,waves,beach,sea,water", 14, ""),
    ("team-meeting", "Team collaborating around a table with laptops", "team,meeting,collaboration,teamwork,office", 12, ""),
    ("retail-shop", "Small shop owner at a retail counter", "shop,retail,store,small business,owner", 11, ""),
    ("card-payment", "Contactless card payment at a counter", "payment,card,contactless,shop,digital payments", 9, ""),
    ("library", "Student reading books in a quiet library", "library,student,books,learning,knowledge,reading", 13, ""),
    ("kitchen", "Cooking vegetables in a home kitchen", "cooking,kitchen,food,vegetables,home", 12, ""),
    ("sunrise-clouds", "Sunrise breaking through clouds", "sunrise,clouds,sky,hope,morning", 15, ""),
    ("storm-sky", "Storm clouds moving across the sky", "storm,clouds,sky,weather,rain", 12, ""),
    ("summit", "Hiker reaching a mountain summit at sunrise", "mountain,summit,hiking,success,achievement", 12, ""),
    ("factory", "Factory production line with machines", "factory,manufacturing,machines,industry,production", 13, ""),
    ("farm", "Green farm field with tractor", "farm,field,agriculture,tractor,crops", 14, ""),
    ("smartphone", "Person scrolling a smartphone in cafe", "smartphone,phone,app,cafe,mobile", 10, ""),
    ("python-snake", "Python snake coiled on a branch", "python,snake,reptile,wildlife", 11, ""),
    ("field-mouse", "Small field mouse in grass", "mouse,rodent,field,wildlife,grass", 9, ""),
    ("clock", "Clock hands moving close-up", "clock,time,deadline,hours", 10, ""),
    ("planner", "Person writing in a daily planner", "planner,habit,writing,desk,routine,discipline", 11, ""),
    ("classroom", "Teacher explaining at a whiteboard", "teacher,classroom,whiteboard,tutorial,education", 12, ""),
    ("delivery", "Delivery van driving through city streets", "delivery,van,logistics,city,shipping", 12, ""),
    ("solar", "Solar panels on a rooftop in sunlight", "solar,energy,panels,renewable,sustainability", 12, ""),
    # ----- deliberately problematic assets -----
    ("cash-pile", "Pile of cash banknotes", "cash,money,banknotes,value,rich", 10, ""),
    ("beer-bar", "Friends drinking beer at a bar", "beer,bar,alcohol,friends,drinks,party", 10, ""),
    ("portrait-face", "Close-up portrait face of a woman smiling", "portrait,face,woman,smiling,closeup,people", 10, "face"),
    ("wm-office", "Office work laptop (preview, watermarked)", "office,laptop,work,business,typing", 12, "watermarked"),
    ("nolicence-city", "City traffic timelapse", "city,traffic,urban,skyline", 12, "nolicence"),
    ("editorial-crowd", "Protest crowd in city (editorial use only)", "crowd,protest,city,people", 12, "editorial"),
    ("corrupt-server", "Server room technology", "server,data center,technology,cloud computing", 12, "corrupt"),
    ("short-coins", "Coins on table", "coins,money,saving", 2, ""),
]
PATTERNS = ["testsrc2", "mandelbrot=maxiter=200", "cellauto=rule=110", "life=ratio=0.2:mold=10"]


def _cand(row) -> FootageCandidate:
    aid, title, tags, dur, special = row
    lic = LICENCE.model_copy()
    if special == "nolicence":
        lic = Licence()
    if special == "editorial":
        lic = lic.model_copy(update={"restrictions": ["editorial use only"]})
    return FootageCandidate(
        provider="mock", asset_id=aid, title=title, tags=tags.split(","),
        source_url=f"https://mock-footage.invalid/videos/{aid}", download_url=f"mock://{aid}",
        thumbnail_url=f"mock://{aid}/thumb", width=1280, height=720, duration_s=float(dur), fps=30,
        licence=lic, attribution=Attribution(required=False, creator="IncuBrix test fixtures",
                                             text="Synthetic fixture clip", url=f"https://mock-footage.invalid/videos/{aid}"),
        watermarked=special == "watermarked", editorial_only=special == "editorial",
        extra={"special": special},
    )


def _src(aid: str, dur: float) -> str:
    h = int(hashlib.md5(aid.encode()).hexdigest()[:8], 16)
    pat = PATTERNS[h % len(PATTERNS)]
    sep = ":" if "=" in pat else "="
    return f"{pat}{sep}s=640x360:r=30"


def _filters(aid: str) -> str:
    h = int(hashlib.md5(aid.encode()).hexdigest()[:8], 16)
    return f"hue=h={h % 360}:s=1.2,scroll=h=0.0025:v=0.0012,scale=1280:720:flags=neighbor,format=yuv420p"


class MockFootageProvider:
    name = "mock"

    def __init__(self, cache_dir: Path) -> None:
        self.cache = cache_dir
        self.cache.mkdir(parents=True, exist_ok=True)
        self.catalog = {r[0]: r for r in CATALOG}

    def search(self, query: str, per_page: int, orientation=None) -> list[FootageCandidate]:
        if apply_search_fault(self.name):
            return []
        q = {w for w in query.lower().replace(",", " ").split() if len(w) > 2}
        scored = []
        for row in CATALOG:
            hay = (row[1] + " " + row[2]).lower()
            hits = sum(1 for w in q if w in hay)
            if hits:
                scored.append((hits, row))
        scored.sort(key=lambda x: -x[0])
        out = []
        for rank, (_, row) in enumerate(scored[:per_page]):
            c = _cand(row)
            c.query, c.provider_rank = query, rank
            out.append(c)
        return out

    def get_asset(self, asset_id: str) -> FootageCandidate | None:
        row = self.catalog.get(asset_id)
        return _cand(row) if row else None

    def download(self, cand: FootageCandidate, dest: Path) -> Path:
        if apply_download_fault(self.name, dest):
            return dest
        if cand.extra.get("special") == "corrupt":
            dest.write_bytes(b"not a video" * 100)
            return dest
        cached = self.cache / f"{cand.asset_id}.mp4"
        if not cached.exists():
            dur = cand.duration_s or 10
            tmp = cached.with_suffix(".part.mp4")
            run_ffmpeg(["-f", "lavfi", "-i", _src(cand.asset_id, dur), "-t", f"{dur}", "-vf", _filters(cand.asset_id),
                        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "26", "-an", str(tmp)])
            tmp.replace(cached)
        dest.write_bytes(cached.read_bytes())
        return dest

    def thumbnail(self, cand: FootageCandidate, dest: Path) -> Path | None:
        dest = dest.with_name(f"{cand.asset_id}-{dest.name}")  # mock face detector keys off the name
        run_ffmpeg(["-f", "lavfi", "-i", _src(cand.asset_id, 3), "-ss", "1", "-frames:v", "1",
                    "-vf", _filters(cand.asset_id).replace(",format=yuv420p", ""), str(dest)])
        return dest
