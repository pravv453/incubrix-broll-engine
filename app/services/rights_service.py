"""Rights gate (must-have #3). A hard pass/fail decision made by code from
recorded evidence, never by an LLM and never traded off against relevance."""
from app.schemas.footage import FootageCandidate

POLICY_VERSION = "rights-policy-v1"
ALLOWED_PROVIDERS = {"mock", "pixabay", "generated"}
ACCEPTED_EVIDENCE = {"provider_policy_v1", "ai_generated_provider_terms"}


def check(c: FootageCandidate) -> list[str]:
    """Return rejection reasons; empty list = eligible for commercial use."""
    r: list[str] = []
    if c.provider not in ALLOWED_PROVIDERS:
        r.append(f"provider '{c.provider}' is not on the rights allowlist")
    if not c.asset_id or not c.source_url:
        r.append("missing asset id or source link")
    if not c.licence.name or not c.licence.url:
        r.append("licence evidence missing (name/url)")
    if not c.licence.commercial_use_allowed:
        r.append("licence does not allow commercial use")
    if c.licence.evidence_status not in ACCEPTED_EVIDENCE:
        r.append(f"licence evidence status '{c.licence.evidence_status}' not accepted")
    if c.watermarked:
        r.append("watermarked footage")
    if c.editorial_only or any("editorial" in x.lower() for x in c.licence.restrictions):
        r.append("editorial-only asset cannot be used commercially")
    if c.attribution.required and not (c.attribution.text or c.attribution.creator):
        r.append("attribution required but not available")
    if c.kind == "generated" and not c.is_ai_generated:
        r.append("generated asset not flagged as AI-generated")
    return r


def completeness(c: FootageCandidate) -> float:
    fields = [c.asset_id, c.source_url, c.licence.name, c.licence.url,
              c.attribution.text or c.attribution.creator, c.retrieved_at]
    return sum(1 for f in fields if f) / len(fields)


def record(c: FootageCandidate) -> dict:
    return {
        "provider": c.provider, "asset_id": c.asset_id, "source_url": c.source_url,
        "licence": c.licence.model_dump(), "attribution": c.attribution.model_dump(),
        "is_ai_generated": c.is_ai_generated, "retrieved_at": c.retrieved_at, "policy_version": POLICY_VERSION,
    }
