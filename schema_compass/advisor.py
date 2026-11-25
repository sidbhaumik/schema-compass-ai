"""Turn a scored result into a plain-language recommendation a business user can act on.

This is deterministic, so it works without an LLM. When Claude reviewed the shortlist, its
summary and load rule are shown alongside, not instead of, these checks.
"""
from __future__ import annotations

DECISIONS = {
    "high": ("use", "Use this source"),
    "medium": ("review", "Likely source, confirm before using"),
    "low": ("research", "Possible source, needs research"),
    "none": ("no_source", "No reliable source found"),
}


def _name(cand: dict) -> str:
    table = cand["source_table"]
    column = cand.get("source_column")
    if column:
        return f"{table['name']}.{column['name']}"
    return table["name"]


def _business_name(cand: dict) -> str:
    item = cand.get("source_column") or cand["source_table"]
    return item.get("logical_name") or ""


def _why(cand: dict, target_is_column: bool) -> list[str]:
    """Short, non-technical reasons drawn from the score components."""
    f = cand["features"]
    why = []
    name, desc = f.get("name") or 0, f.get("description") or 0
    sem = f.get("semantic")
    if name >= 0.6:
        why.append("Its name means the same thing as the target's once abbreviations are expanded.")
    elif name >= 0.35:
        why.append("Its name partly overlaps with the target's.")
    if desc >= 0.35 or (sem is not None and sem >= 0.6):
        why.append("Its business description says the same thing.")
    if target_is_column:
        if (f.get("table_context") or 0) >= 0.6:
            why.append("It sits in the source table that best matches the target table.")
        if f.get("type_compatibility") == 1:
            why.append("The data types match.")
    elif (f.get("column_coverage") or 0) >= 0.6:
        why.append("Most of the target's columns have a clear counterpart in this table.")
    for ev in cand["evidence"]:
        if ev["strength"] <= 0:
            continue
        if ev["kind"] == "feedback":
            why.append(f"A reviewer already approved this mapping ({ev['source']}).")
        elif ev["kind"] == "s2t":
            why.append(f"An earlier source-to-target sheet maps it this way ({ev['source']}).")
        else:
            why.append(f"Team discussion supports it ({ev['source']}).")
    return why or ["It is the closest match by name and description, but no signal is strong."]


def _checks(cand: dict, result: dict) -> list[str]:
    """What the user should verify or do before relying on the candidate."""
    checks = []
    for issue in cand["issues"]:
        lowered = issue.lower()
        if "width" in lowered:
            checks.append("Check the longest values in the source; some may not fit the target column.")
        elif "boolean" in lowered or "mapping" in lowered:
            checks.append("Agree how values convert (for example 'Y'/'N' to true/false) and how blanks are handled.")
        elif "truncate" in lowered:
            checks.append("Source has decimals the target cannot hold; confirm rounding rules.")
        elif "time zone" in lowered or "timestamp" in lowered:
            checks.append("Confirm the time zone before converting timestamps to dates.")
        elif "datatype families differ" in lowered:
            checks.append("The data types are different kinds; this is probably not a direct load.")
        elif "roles differ" in lowered:
            checks.append("One looks like an ID and the other like a name or amount; confirm they hold the same values.")
        elif "join or derivation" in lowered:
            checks.append("This field lives in an unrelated table; it would need a join, so involve a data engineer.")
        elif "incomplete" in lowered:
            checks.append("Datatype information is missing on one side; check it with the data owner.")
        else:
            checks.append(issue)
    for ev in cand["evidence"]:
        if ev["strength"] < 0 and ev["kind"] != "feedback":
            checks.append(f"Read the warning in {ev['source']}: \"{ev['snippet'][:160]}\"")
        elif ev["strength"] == 0 and ev["snippet"]:
            checks.append(f"Note from {ev['source']}: \"{ev['snippet'][:160]}\"")
    if cand["band"] in {"medium", "low"}:
        checks.append("Compare a sample of values from source and target before approving.")
    llm_rule = result["recommendation"].get("transformation")
    if llm_rule:
        checks.append(f"Suggested load rule: {llm_rule}")
    seen, unique = set(), []
    for c in checks:
        if c not in seen:
            seen.add(c)
            unique.append(c)
    return unique


def advise(result: dict) -> dict:
    rec = result["recommendation"]
    target = result["target"]
    target_is_column = target["column"] is not None
    target_name = target["table"]["name"] + (f".{target['column']['name']}" if target_is_column else "")
    decision, label = DECISIONS[rec["band"]]
    cands = result["candidates"]
    if not cands or decision == "no_source":
        reasons = [w["summary"] for w in result.get("warnings", [])]
        if cands:
            reasons.append(f"The closest candidate, {_name(cands[0])}, scored only {cands[0]['score']:.0f}/100.")
        return dict(decision=decision, label=label,
                    headline=f"No reliable source was found for {target_name}.",
                    why=reasons or ["Nothing in the source metadata or documents describes this field."],
                    checks=["Ask the source data owner whether this data exists under another name.",
                            "If it is new in the target, mark it as a new attribute or derived field."],
                    alternatives=[])
    top = cands[0]
    business = _business_name(top)
    ref = _name(top) + (f" ({business})" if business else "")
    headline = {
        "use": f"Use {ref}",
        "review": f"{ref} is the likely source. Confirm before using it.",
        "research": f"{ref} might be the source, but the evidence is weak.",
    }[decision]
    alternatives = [dict(name=_name(c), score=c["score"], business_name=_business_name(c))
                    for c in cands[1:3] if c["score"] >= 30]
    checks = _checks(top, result)
    if rec["margin"] < 8 and alternatives:
        checks.insert(0, f"{alternatives[0]['name']} scored almost as high; rule it out first.")
    return dict(decision=decision, label=label, headline=headline, why=_why(top, target_is_column),
                checks=checks, alternatives=alternatives)
