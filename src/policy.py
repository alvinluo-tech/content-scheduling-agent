from __future__ import annotations

from typing import Any

STATUS_PASS = "pass"
STATUS_WARN = "pass_with_warnings"
STATUS_RISK = "risk"
STATUS_MISSING = "missing_info"

SAVABLE = {STATUS_PASS, STATUS_WARN}


def is_blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    if isinstance(value, (list, dict)):
        return len(value) == 0
    return False


def _violation(rule: dict[str, Any], kind: str, reason: str) -> dict[str, Any]:
    return {
        "rule_id": rule["id"],
        "rule": rule["name"],
        "kind": kind,
        "severity": rule["severity"],
        "reason": reason,
    }


def evaluate_rules(material: dict[str, Any], rules_doc: dict[str, Any]) -> dict[str, Any]:
    violations: list[dict[str, Any]] = []
    missing_fields: list[str] = []
    manual_review = False

    for rule in rules_doc["rules"]:
        rtype = rule["type"]
        if rtype == "required_field":
            field = rule["field"]
            if is_blank(material.get(field)):
                missing_fields.append(field)
                violations.append(_violation(rule, "missing_field", f"{rule['message']}（缺 {field}）"))
        elif rtype == "must_be_true":
            field = rule["field"]
            value = material.get(field)
            if value is None:
                missing_fields.append(field)
                violations.append(_violation(rule, "missing_field", f"{rule['message']}（{field} 状态未知）"))
            elif value is not True:
                violations.append(_violation(rule, "policy_violation", rule["message"]))
        elif rtype == "banned_terms":
            title = str(material.get("title") or "")
            hits = [term for term in rules_doc["banned_terms"] if term in title]
            if hits:
                violations.append(_violation(rule, "policy_violation", f"{rule['message']}：{('、'.join(hits))}"))
        elif rtype == "max_length":
            text = str(material.get(rule["field"]) or "")
            if len(text) > rule["limit"]:
                violations.append(_violation(rule, "policy_warning", f"{rule['message']}（当前 {len(text)} 字，上限 {rule['limit']}）"))
        elif rtype == "manual_review_category":
            if material.get("category") in rule["values"]:
                manual_review = True
                violations.append(_violation(rule, "manual_review", rule["message"]))

    if missing_fields:
        status = STATUS_MISSING
    elif any(v["severity"] in ("blocking", "manual_review") for v in violations):
        status = STATUS_RISK
    elif violations:
        status = STATUS_WARN
    else:
        status = STATUS_PASS

    return {
        "review_status": status,
        "violations": violations,
        "missing_fields": missing_fields,
        "requires_manual_review": manual_review,
        "can_draft": status in SAVABLE,
    }


def to_minutes(hhmm: str) -> int:
    hour, minute = hhmm.split(":")
    return int(hour) * 60 + int(minute)


def to_hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def slot_grid(hours: dict[str, Any]) -> list[str]:
    start, end, step = to_minutes(hours["start"]), to_minutes(hours["end"]), int(hours["granularity_min"])
    return [to_hhmm(m) for m in range(start, end, step)]


def find_conflicts(occupied: list[dict[str, Any]], time: str) -> list[dict[str, Any]]:
    return [slot for slot in occupied if slot["time"] == time]


def next_free_slots(hours: dict[str, Any], occupied: list[dict[str, Any]], after: str, limit: int = 3) -> list[str]:
    taken = {slot["time"] for slot in occupied}
    grid = slot_grid(hours)
    start = to_minutes(after)
    return [time for time in grid if to_minutes(time) > start and time not in taken][:limit]
