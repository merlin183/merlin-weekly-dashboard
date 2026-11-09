"""Installer-owned defaults. No organization, person or integration is preconfigured."""
import json
import math
import os
import re
from datetime import date


def env_bool(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def env_list(name: str) -> list[str]:
    return [part.strip() for part in os.environ.get(name, "").split(",") if part.strip()]


def env_dates(name: str) -> frozenset[str]:
    values = env_list(name)
    for value in values:
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
            raise ValueError(f"{name} must contain only ISO dates (YYYY-MM-DD)")
        date.fromisoformat(value)
    return frozenset(values)


def require_jwt_secret(value: str) -> str:
    """Require an independently generated signing key; never silently sign with empty keys."""
    value = value.strip()
    if len(value.encode("utf-8")) < 32 or len(set(value)) < 12 or any(
        word in value.lower() for word in ("change-me", "changeme", "placeholder", "your-secret", "example-secret")
    ):
        raise ValueError("JWT_SECRET must be an independently generated secret of at least 32 bytes")
    return value


BACKGROUND_TASKS_ENABLED = env_bool("BACKGROUND_TASKS_ENABLED")
STORAGE_AUTO_CREATE_BUCKET = env_bool("STORAGE_AUTO_CREATE_BUCKET")
LEGACY_SHARED_LOGIN_ENABLED = env_bool("LEGACY_SHARED_LOGIN_ENABLED")
EGRESS_DIAGNOSTICS_ENABLED = env_bool("EGRESS_DIAGNOSTICS_ENABLED")
LEAVE_DEFAULT_PM = os.environ.get("LEAVE_DEFAULT_PM", "").strip()
LEAVE_DEFAULT_ADMIN = os.environ.get("LEAVE_DEFAULT_ADMIN", "").strip()
SINGLE_STAGE_DEFAULT_ADMIN = os.environ.get("SINGLE_STAGE_DEFAULT_ADMIN", "").strip()
EXPENSE_NOTIFY_NAMES = env_list("EXPENSE_NOTIFY_NAMES")
LINEAR_EXCLUDED_TEAM_NAMES = env_list("LINEAR_EXCLUDED_TEAM_NAMES")
HIDDEN_PROFILE_NAMES = env_list("HIDDEN_PROFILE_NAMES")
CF_CUTOVER_DATE = os.environ.get("METRICS_LLM_AVAILABLE_FROM", "").strip()
METRICS_SKIPPED_DATES = env_dates("METRICS_SKIPPED_DATES")


def team_config_defaults() -> dict:
    return {
        "members": [],
        "profileVisibility": {"hiddenNames": list(HIDDEN_PROFILE_NAMES)},
        "approvalDefaults": {"primary": LEAVE_DEFAULT_PM, "reference": LEAVE_DEFAULT_ADMIN},
    }


def llm_pricing() -> dict:
    """Optional installer pricing, keyed by model, in USD per million tokens."""
    raw = os.environ.get("LLM_PRICING_JSON", "").strip()
    if not raw:
        return {}
    pricing = json.loads(raw)
    if not isinstance(pricing, dict):
        raise ValueError("LLM_PRICING_JSON must be a JSON object")
    normalized = {}
    for model, rates in pricing.items():
        if not re.fullmatch(r"[a-zA-Z0-9_.:/-]{1,200}", model) or not isinstance(rates, dict):
            raise ValueError("LLM_PRICING_JSON contains an invalid model or rate object")
        normalized[model] = {}
        for key in ("input", "output", "cache_read", "cache_write"):
            value = float(rates.get(key, 0))
            if not math.isfinite(value) or value < 0:
                raise ValueError("LLM pricing values must be finite and nonnegative")
            normalized[model][key] = value
    return normalized


def bq_cost_case() -> str:
    clauses = []
    for model, rates in llm_pricing().items():
        clauses.append(
            f"WHEN '{model}' THEN (prompt_tokens*{rates['input']} + completion_tokens*{rates['output']} "
            f"+ IFNULL(cache_read_tokens,0)*{rates['cache_read']} "
            f"+ IFNULL(cache_creation_tokens,0)*{rates['cache_write']}) / 1e6"
        )
    return "CASE model " + " ".join(clauses) + " ELSE 0 END" if clauses else "0.0"
