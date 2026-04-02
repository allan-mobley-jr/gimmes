"""gimmes config — interactive configuration wizard driven by model introspection."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import get_origin

import typer
from pydantic import BaseModel
from pydantic.fields import FieldInfo
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from gimmes.config import (
    CONFIG_SECTIONS,
    GIMMES_HOME,
    ScoringWeights,
    config_keys_in_db,
    save_config_value,
)

console = Console()

DB_PATH = GIMMES_HOME / "gimmes.db"


# ---------------------------------------------------------------------------
# Setting — derived from Pydantic field introspection
# ---------------------------------------------------------------------------


@dataclass
class Setting:
    """Metadata for a single configuration setting, derived from a Pydantic field."""

    key: str  # Dotted key, e.g. "strategy.gimme_threshold"
    name: str  # Human-readable name
    description: str  # Plain-language explanation
    type: str  # "int", "float", "str", "list"
    default: int | float | str | list[str]
    min_val: float | None = None
    max_val: float | None = None
    choices: list[str] = field(default_factory=list)

    @property
    def section(self) -> str:
        return self.key.split(".")[0]


def _field_type_str(field_info: FieldInfo) -> str:
    """Derive the wizard type string from a Pydantic field's annotation."""
    ann = field_info.annotation
    if ann is int:
        return "int"
    if ann is float:
        return "float"
    if ann is str:
        return "str"
    origin = get_origin(ann)
    if origin is list:
        return "list"
    # Check if it's a BaseModel subclass (nested model like ScoringWeights)
    if isinstance(ann, type) and issubclass(ann, BaseModel):
        return "model"
    return "str"


def _field_to_setting(key_prefix: str, field_name: str, field_info: FieldInfo) -> Setting | None:
    """Convert a Pydantic FieldInfo into a Setting for the wizard."""
    extra = field_info.json_schema_extra or {}
    if "display_name" not in extra:
        return None  # Field has no wizard metadata — skip it

    dotted_key = f"{key_prefix}.{field_name}" if key_prefix else field_name
    type_str = _field_type_str(field_info)
    if type_str == "model":
        return None  # Nested models are recursed into, not prompted directly

    from pydantic_core import PydanticUndefined

    default = field_info.default
    if default is PydanticUndefined:
        if field_info.default_factory is not None:
            default = field_info.default_factory()
        else:
            default = None

    return Setting(
        key=dotted_key,
        name=extra["display_name"],
        description=extra.get("description", ""),
        type=type_str,
        default=default,
        min_val=extra.get("min_val"),
        max_val=extra.get("max_val"),
        choices=extra.get("choices", []),
    )


def _iter_section_settings(
    section_key: str, model_cls: type[BaseModel],
) -> list[Setting]:
    """Iterate fields in a sub-model and yield Settings for the wizard."""
    settings: list[Setting] = []
    for fname, finfo in model_cls.model_fields.items():
        # Handle nested model (e.g. ScoringConfig.weights -> ScoringWeights)
        ann = finfo.annotation
        if isinstance(ann, type) and issubclass(ann, BaseModel):
            nested_prefix = f"{section_key}.{fname}"
            for nested_name, nested_info in ann.model_fields.items():
                s = _field_to_setting(nested_prefix, nested_name, nested_info)
                if s is not None:
                    settings.append(s)
        else:
            s = _field_to_setting(section_key, fname, finfo)
            if s is not None:
                settings.append(s)
    return settings


def _iter_sections() -> Iterator[tuple[str, str, str, list[Setting]]]:
    """Yield (section_key, display_name, description, settings) for each section."""
    for field_name, model_cls in CONFIG_SECTIONS:
        extra = model_cls.model_config.get("json_schema_extra", {})
        section_name = extra.get("section_name", field_name.replace("_", " ").title())
        section_desc = extra.get("section_description", "")
        settings = _iter_section_settings(field_name, model_cls)
        if settings:
            yield field_name, section_name, section_desc, settings


# Section keys for validation
SECTION_KEYS = [key for key, _ in CONFIG_SECTIONS]


# ---------------------------------------------------------------------------
# Input helpers
# ---------------------------------------------------------------------------


def _format_list_grouped(values: list[object], categories: dict[str, list[str]]) -> str:
    """Format a list with items grouped under category headings."""
    value_set = {str(v) for v in values}
    parts: list[str] = []
    categorised: set[str] = set()
    for cat_name, cat_tickers in categories.items():
        present = [t for t in cat_tickers if t in value_set]
        if not present:
            continue
        categorised.update(present)
        parts.append(f"\n  {cat_name} ({len(present)})\n    {', '.join(present)}")
    other = [str(v) for v in values if str(v) not in categorised]
    if other:
        parts.append(f"\n  Other ({len(other)})\n    {', '.join(other)}")
    return "".join(parts)


def _format_current(
    value: object,
    setting: Setting,
    *,
    full: bool = False,
    categories: dict[str, list[str]] | None = None,
) -> str:
    """Format a value for display."""
    if setting.type == "list" and isinstance(value, list):
        if full and categories:
            return _format_list_grouped(value, categories)
        if full:
            return "\n  " + "\n  ".join(str(v) for v in value)
        if len(value) > 6:
            return f"[{len(value)} items] {', '.join(str(v) for v in value[:6])}..."
        return ", ".join(str(v) for v in value)
    if setting.type == "float" and isinstance(value, float):
        if value < 1:
            return f"{value:.2f}"
        return f"{value:,.2f}"
    return str(value)


def _check_range(
    val: int | float,
    lo: float | None,
    hi: float | None,
    fmt: type = float,
) -> None:
    """Raise ValueError if *val* is outside [lo, hi]. Uses *fmt* for display."""
    below = lo is not None and val < lo
    above = hi is not None and val > hi
    if not below and not above:
        return
    if lo is not None and hi is not None:
        raise ValueError(f"Must be between {fmt(lo)} and {fmt(hi)}")
    if below:
        raise ValueError(f"Must be at least {fmt(lo)}")
    raise ValueError(f"Must be at most {fmt(hi)}")


def _parse_input(raw: str, setting: Setting) -> int | float | str | list[str]:
    """Parse and validate user input for a setting. Raises ValueError on bad input."""
    raw = raw.strip()

    if setting.type == "int":
        val = int(raw)
        _check_range(val, setting.min_val, setting.max_val, fmt=int)
        return val

    if setting.type == "float":
        val = float(raw)
        _check_range(val, setting.min_val, setting.max_val)
        return val

    if setting.type == "str":
        if setting.choices and raw not in setting.choices:
            raise ValueError(f"Must be one of: {', '.join(setting.choices)}")
        return raw

    if setting.type == "list":
        stripped = raw.strip()
        if stripped.startswith("["):
            try:
                parsed = json.loads(stripped)
                if isinstance(parsed, list):
                    return [str(item).strip() for item in parsed if str(item).strip()]
            except json.JSONDecodeError:
                pass  # Fall through to comma-split
        return [item.strip() for item in raw.split(",") if item.strip()]

    return raw


def _prompt_setting(
    setting: Setting, current_value: object, *, is_new: bool = False,
) -> object:
    """Prompt the user for a single setting. Returns the new value or current if skipped."""
    display = _format_current(current_value, setting)

    if is_new:
        label = f"[bold yellow] NEW [/bold yellow] [bold]{setting.name}[/bold]"
    else:
        label = f"[bold]{setting.name}[/bold]"
    console.print(f"\n  {label}")
    console.print(f"  Current value: [cyan]{display}[/cyan]")

    # Show description indented
    for line in setting.description.split("\n"):
        console.print(f"  [dim]{line}[/dim]")

    if setting.choices:
        console.print(f"  [dim]Options: {', '.join(setting.choices)}[/dim]")

    if setting.type == "list":
        hint = "comma-separated list, or Enter to keep"
    else:
        hint = "Enter to keep current"

    while True:
        raw = typer.prompt(f"  New value ({hint})", default="", show_default=False)
        if raw == "":
            return current_value
        try:
            return _parse_input(raw, setting)
        except ValueError as e:
            console.print(f"  [red]Invalid: {e}. Try again.[/red]")


def _get_current_value(overrides: dict, dotted_key: str, default: object) -> object:
    """Get a value from the nested overrides dict, falling back to the default."""
    parts = dotted_key.split(".")
    current: object = overrides
    for part in parts:
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return default
    return current


def _scoring_weights_total(overrides: dict) -> float:
    """Compute the sum of scoring weights from overrides (falling back to defaults)."""
    weights_section = overrides.get("scoring", {}).get("weights", {})
    return sum(
        float(weights_section.get(fname, finfo.default))
        for fname, finfo in ScoringWeights.model_fields.items()
        if isinstance(weights_section.get(fname, finfo.default), (int, float))
    )


# ---------------------------------------------------------------------------
# Key resolution & validation (shared by wizard and CLI)
# ---------------------------------------------------------------------------


def resolve_setting(key: str) -> Setting:
    """Find the Setting matching a dotted key (e.g. 'strategy.gimme_threshold').

    Raises ValueError with a list of valid keys if not found.
    """
    for _section_key, _section_name, _section_desc, settings in _iter_sections():
        for s in settings:
            if s.key == key:
                return s

    all_keys = sorted(
        s.key
        for _, _, _, settings in _iter_sections()
        for s in settings
    )
    raise ValueError(
        f"Unknown config key: {key}\n"
        f"Valid keys: {', '.join(all_keys)}"
    )


def validate_config_value(key: str, raw_value: str) -> tuple[Setting, object]:
    """Resolve a dotted key and validate/parse a raw string value.

    Returns (setting, parsed_value). Raises ValueError on bad key or value.
    """
    setting = resolve_setting(key)
    parsed = _parse_input(raw_value, setting)
    return setting, parsed


# ---------------------------------------------------------------------------
# Main wizard
# ---------------------------------------------------------------------------


def run_config_wizard(
    section_filter: str | None = None, *, new_only: bool = False,
) -> None:
    """Run the interactive config wizard, reading/writing the database."""
    from gimmes.config import _load_config_from_db

    if not DB_PATH.exists():
        console.print(
            f"[red]Database not found at {DB_PATH}[/red]\n"
            "Run [bold]gimmes init[/bold] first to create it."
        )
        raise typer.Exit(1)

    overrides = _load_config_from_db(DB_PATH)
    existing_keys = config_keys_in_db(DB_PATH)

    if section_filter:
        if section_filter not in SECTION_KEYS:
            console.print(
                f"[red]Unknown section: {section_filter}[/red]\n"
                f"Valid sections: {', '.join(SECTION_KEYS)}"
            )
            raise typer.Exit(1)

    if new_only:
        # Early exit when --new-only has nothing to show
        all_keys = {
            s.key
            for _, _, _, settings in _iter_sections()
            for s in settings
        }
        new_keys = all_keys - existing_keys
        if section_filter:
            new_keys = {k for k in new_keys if k.startswith(f"{section_filter}.")}
        if not new_keys:
            console.print(
                "[green]All settings are already configured."
                " Nothing new to set up.[/green]"
            )
            return
        console.print("\n[bold cyan]GIMMES Configuration Wizard — New Settings Only[/bold cyan]")
        console.print("[dim]Only settings added since your last configuration are shown.[/dim]\n")
    else:
        console.print("\n[bold cyan]GIMMES Configuration Wizard[/bold cyan]")
        console.print(
            "[dim]Walk through each setting. Press Enter to keep the current value.[/dim]\n"
        )

    changed = False

    for section_key, section_name, section_desc, settings in _iter_sections():
        if section_filter and section_key != section_filter:
            continue

        if new_only and not any(s.key not in existing_keys for s in settings):
            continue

        header = Text(f" {section_name} ", style="bold white on blue")
        console.print(Panel(header, subtitle=section_desc, expand=True))

        for setting in settings:
            current = _get_current_value(overrides, setting.key, setting.default)
            is_new = setting.key not in existing_keys

            if new_only and not is_new:
                continue

            new_value = _prompt_setting(setting, current, is_new=is_new)
            value_changed = new_value != current
            # In --new-only mode, persist defaults so the setting is
            # marked as configured and won't reappear on the next run.
            if value_changed or (new_only and is_new):
                save_config_value(setting.key, new_value, db_path=DB_PATH)
                # Update in-memory overrides for scoring validation
                parts = setting.key.split(".")
                d = overrides
                for part in parts[:-1]:
                    d = d.setdefault(part, {})
                d[parts[-1]] = new_value
                if value_changed:
                    changed = True
                    display = _format_current(new_value, setting)
                    console.print(f"  [green]Updated to: {display}[/green]")

    # Validate scoring weights if any were touched
    scoring_touched = section_filter is None or section_filter == "scoring"
    if scoring_touched:
        total = _scoring_weights_total(overrides)
        if abs(total - 1.0) > 0.01:
            console.print(
                f"\n[yellow]Warning: Scoring weights sum to {total:.2f} instead of 1.00.[/yellow]\n"
                "The system will still work, but scores won't be properly normalized.\n"
                "Consider adjusting weights so they add up to 1.0."
            )

    if changed:
        console.print(f"\n[bold green]Configuration saved to database ({DB_PATH})[/bold green]")
    else:
        console.print("\n[dim]No changes made.[/dim]")
