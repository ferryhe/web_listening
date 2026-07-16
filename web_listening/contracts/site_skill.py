from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator, model_validator

from web_listening.contracts._protocol import (
    ExecutorId,
    JsonObject,
    NonEmptyString,
    SkillVersion,
    StrictContractModel,
    require_aware_timestamp,
    validate_domain,
    validate_entrypoint,
    validate_portable_json_field,
    validate_profile_ref,
    validate_script_path,
)


SiteSkillStatus = Literal["draft", "probed", "reviewed", "active", "deprecated"]


class SecretPolicy(StrictContractModel):
    allow_secret_references: bool
    forbid_secret_values: Literal[True]
    allowed_reference_schemes: tuple[NonEmptyString, ...] = ()

    @field_validator("allowed_reference_schemes")
    @classmethod
    def validate_unique_schemes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("allowed_reference_schemes must be unique")
        return value

    @model_validator(mode="after")
    def validate_reference_schemes(self) -> SecretPolicy:
        if self.allow_secret_references and not self.allowed_reference_schemes:
            raise ValueError(
                "allowed_reference_schemes must be non-empty when references are allowed"
            )
        if not self.allow_secret_references and self.allowed_reference_schemes:
            raise ValueError(
                "allowed_reference_schemes must be empty when references are forbidden"
            )
        return self


class VerificationRule(StrictContractModel):
    rule_id: NonEmptyString
    description: NonEmptyString


class RuntimeRequirement(StrictContractModel):
    requirement_id: NonEmptyString
    description: NonEmptyString
    optional: bool = False


class SiteSkillExecutor(StrictContractModel):
    executor_id: ExecutorId
    enabled: bool = True
    config: JsonObject = Field(default_factory=dict)
    script_path: str | None = None

    _validate_config = field_validator("config")(validate_portable_json_field)
    _validate_script_path = field_validator("script_path")(validate_script_path)


class SiteSkillRecipe(StrictContractModel):
    recipe_id: NonEmptyString
    enabled: bool = True
    executor_id: ExecutorId
    profile_ref: str
    entrypoint: str
    output_contract: Literal["capture-result.v1"] = "capture-result.v1"
    required_capabilities: tuple[NonEmptyString, ...] = Field(min_length=1)
    verification_rules: tuple[VerificationRule, ...] = Field(min_length=1)

    _validate_profile_ref = field_validator("profile_ref")(validate_profile_ref)
    _validate_entrypoint = field_validator("entrypoint")(validate_entrypoint)

    @model_validator(mode="after")
    def validate_unique_values(self) -> SiteSkillRecipe:
        for name, values in (
            ("required_capabilities", self.required_capabilities),
            ("verification_rules", [rule.rule_id for rule in self.verification_rules]),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"{name} values must be unique")
        return self


class SiteSkill(StrictContractModel):
    schema_version: Literal["site-skill.v1"] = "site-skill.v1"
    skill_id: NonEmptyString
    site_key: NonEmptyString
    version: SkillVersion
    status: SiteSkillStatus
    generated_at: datetime
    runtime_requirements: tuple[RuntimeRequirement, ...] = Field(min_length=1)
    secret_policy: SecretPolicy
    allowed_domains: tuple[str, ...] = Field(min_length=1)
    default_executor_id: ExecutorId
    default_recipe_id: NonEmptyString
    executors: tuple[SiteSkillExecutor, ...] = Field(min_length=1)
    recipes: tuple[SiteSkillRecipe, ...] = Field(min_length=1)
    metadata: JsonObject = Field(default_factory=dict)

    _validate_generated_at = field_validator("generated_at")(require_aware_timestamp)
    _validate_domains = field_validator("allowed_domains")(
        lambda values: tuple(validate_domain(v) for v in values)
    )
    _validate_metadata = field_validator("metadata")(validate_portable_json_field)

    @model_validator(mode="after")
    def validate_manifest_consistency(self) -> SiteSkill:
        requirement_ids = [item.requirement_id for item in self.runtime_requirements]
        if len(requirement_ids) != len(set(requirement_ids)):
            raise ValueError("runtime_requirements values must be unique")
        if len(self.allowed_domains) != len(set(self.allowed_domains)):
            raise ValueError("allowed_domains must be unique")
        executor_ids = [executor.executor_id for executor in self.executors]
        if len(executor_ids) != len(set(executor_ids)):
            raise ValueError("executors must have unique executor_id values")
        enabled_executors = {
            item.executor_id for item in self.executors if item.enabled
        }
        if self.default_executor_id not in enabled_executors:
            raise ValueError("default_executor_id must identify an enabled executor")
        recipe_ids = [recipe.recipe_id for recipe in self.recipes]
        if len(recipe_ids) != len(set(recipe_ids)):
            raise ValueError("recipes must have unique recipe_id values")
        enabled_recipes = {
            item.recipe_id: item for item in self.recipes if item.enabled
        }
        if self.default_recipe_id not in enabled_recipes:
            raise ValueError("default_recipe_id must identify an enabled recipe")
        for recipe in self.recipes:
            if recipe.enabled and recipe.executor_id not in enabled_executors:
                raise ValueError("enabled recipes must reference enabled executors")
        if (
            enabled_recipes[self.default_recipe_id].executor_id
            != self.default_executor_id
        ):
            raise ValueError("default recipe and default executor must match")
        return self


__all__ = [
    "RuntimeRequirement",
    "SecretPolicy",
    "SiteSkill",
    "SiteSkillExecutor",
    "SiteSkillRecipe",
    "SiteSkillStatus",
    "VerificationRule",
]
