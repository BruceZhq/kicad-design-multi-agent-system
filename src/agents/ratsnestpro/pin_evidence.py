"""Conservative pin-label equivalence and actionable evidence differences."""
import re


def functions(label: str) -> set[str]:
    # Brackets/slashes denote documented aliases; GPIO suffixes name mux
    # functions. Never erase NC: a conditional NC requires its own evidence.
    label = re.split(r"\s+[—–]\s+", label, maxsplit=1)[0]
    values = re.split(r"[/,\[\]]", label)
    return {re.sub(r"[^a-z0-9]", "", re.sub(r"^(P[A-Z]\d+)-.+$", r"\1", v.strip(), flags=re.I).lower())
            for v in values if v.strip()}


def pin_differences(rows, table):
    observed = {str(p.get("number")): p for p in (table or {}).get("pins", [])}
    differences = []
    for row in rows:
        pin = observed.get(str(row["number"]), {})
        seen = set().union(*(functions(f) for f in pin.get("functions", []) if isinstance(f, str)))
        expected = functions(str(row["name"]))
        if not expected or not expected.issubset(seen):
            differences.append({"number": str(row["number"]), "symbol_name": row["name"],
                                "observed_functions": pin.get("functions", []), "page": pin.get("page"),
                                "reason": "symbol_electrical_type_conflict" if row.get("type") == "no_connect" and seen and "nc" not in seen
                                else "conditional_pin_requires_evidence" if "nc" in expected ^ seen
                                else "pin_function_mismatch"})
    return differences
