"""Token substitution + bracket sanitiser (Samus STANDARD)."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

__tier__ = "STANDARD"


class TokenSubstitutionError(ValueError):
    pass


_TOKEN_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
_FORBIDDEN_BRACKET_PREFIXES: tuple[str, ...] = (
    "AUTONOMY", "SYSTEM", "USER", "ASSISTANT", "TOOL", "SOVEREIGN", "OPERATOR",
)


@dataclass(frozen=True)
class EnrichmentResolver:
    whitelist: frozenset[str] = frozenset({"ai_name", "user_name"})
    strip_inner_braces: bool = True

    def render(self, template: str, variables: dict[str, str]) -> str:
        if not isinstance(template, str):
            raise TokenSubstitutionError("template must be a string")

        def _replace(match: "re.Match[str]") -> str:
            name = match.group(1)
            if name not in self.whitelist:
                return match.group(0)
            value = variables.get(name)
            if value is None:
                return match.group(0)
            if not isinstance(value, str):
                raise TokenSubstitutionError(
                    f"token {name!r} value must be a string, got {type(value).__name__}"
                )
            if self.strip_inner_braces:
                value = value.replace("{", "").replace("}", "")
            return value

        return _TOKEN_RE.sub(_replace, template)

    def sanitise(self, text: str) -> str:
        if not isinstance(text, str) or not text:
            return ""
        prefix_alt = "|".join(re.escape(p) for p in _FORBIDDEN_BRACKET_PREFIXES)
        pattern = re.compile(
            rf"\[(?:{prefix_alt})(?:\|[^\]]*)?\]",
            flags=re.IGNORECASE,
        )
        return pattern.sub("", text)

    def render_and_sanitise(self, template: str, variables: dict[str, str]) -> str:
        return self.sanitise(self.render(template, variables))


def forbidden_bracket_prefixes() -> Iterable[str]:
    return _FORBIDDEN_BRACKET_PREFIXES


__all__ = ["EnrichmentResolver", "TokenSubstitutionError", "forbidden_bracket_prefixes"]
