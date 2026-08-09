"""Provider-neutral prompt and structured output contract for skill routing."""

from __future__ import annotations

from typing import Any

from woon_core.errors import WoonError
from woon_core.skills.service import CatalogSkill


def routing_prompt(catalog: tuple[CatalogSkill, ...], prompts: dict[str, str]) -> str:
    """Build the same routing request for every supported executor."""
    skill_lines = "\n".join(
        f"- {skill.name}: {skill.description}"
        for skill in sorted(catalog, key=lambda item: item.name)
    )
    case_lines = "\n".join(
        f"- {identifier}: {prompt}" for identifier, prompt in sorted(prompts.items())
    )
    return f"""You are evaluating natural-language routing for a skill catalog.
For each case, select only the skill names whose descriptions directly match the request.
Select the smallest sufficient set. Do not invent names. Do not perform the request.
Return every case exactly once in the required JSON shape.

Available skills:
{skill_lines}

Cases:
{case_lines}
"""


def routing_schema(identifiers: list[str], skill_names: list[str]) -> dict[str, Any]:
    """Return the strict JSON schema shared by Codex and Claude."""
    return {
        "type": "object",
        "properties": {
            "cases": {
                "type": "array",
                "minItems": len(identifiers),
                "maxItems": len(identifiers),
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "enum": identifiers},
                        "skills": {
                            "type": "array",
                            "items": {"type": "string", "enum": skill_names},
                        },
                    },
                    "required": ["id", "skills"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["cases"],
        "additionalProperties": False,
    }


def parse_routing_payload(payload: object) -> dict[str, list[str]]:
    """Normalize executor output while rejecting malformed case values."""
    if not isinstance(payload, dict) or not isinstance(payload.get("cases"), list):
        raise WoonError("routing evaluation returned invalid structured output")
    result: dict[str, list[str]] = {}
    for item in payload["cases"]:
        if not isinstance(item, dict):
            raise WoonError("routing evaluation returned invalid structured output")
        identifier = item.get("id")
        skills = item.get("skills")
        if (
            not isinstance(identifier, str)
            or not isinstance(skills, list)
            or any(not isinstance(skill, str) for skill in skills)
            or identifier in result
        ):
            raise WoonError("routing evaluation returned invalid structured output")
        result[identifier] = skills
    return result
