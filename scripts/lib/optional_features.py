"""ADR-092: a feature is on and fails closed, or is named off, and what falls away is a row of
``waived_guarantees``. An unknown or unreadable value raises :class:`OptionalFeatureRefused`, never
reads as off. One :func:`snapshot` per action, passed on (never cached here); one list builder."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Dict, List, Mapping, Optional, Tuple

import config_registry
import config_runtime
import forge_protection_drift as fpd
import gate_profile
from dispatch_spec import ReviewGateConfigError

CI, PLAN_GATE, BRANCH_PROTECTION = "ci", "plan_gate", "branch_protection"
PLAN_GATE_KEY, FILE_KEY = "VNX_PLAN_GATE_ENFORCE", "file"  # branch_protection's config is the file itself
_FALSY = frozenset({"0", "false", "no", "off"})


class OptionalFeatureRefused(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        self.code, self.detail = code, detail
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True)
class Feature:
    name: str
    key: str
    on: Tuple[str, ...]
    off: Mapping[str, str]  # off value -> the guarantee that falls away


FEATURES: Tuple[Feature, ...] = (
    Feature(CI, gate_profile.CI_GATE_KEY, ("1",), {"0": "merged code is tested on exactly this head"}),
    Feature(PLAN_GATE, PLAN_GATE_KEY, ("required",), {
        "advisory": "a build without a green plan is not refused",
        "off": "the plan is not assessed before the build"}),
    Feature(BRANCH_PROTECTION, FILE_KEY, (fpd.ENFORCEMENT_ENFORCE,), {
        "absent": "no server refusal of a direct push and no required vnx-gate/* check",
        fpd.ENFORCEMENT_WARN: "the live server protection is not enforced: a drift from the file "
                              "merges with a warning. The checks in the file are declared, not verified",
        fpd.ENFORCEMENT_OFF: "the door does not read the server protection. The checks in the file are "
                             "declared, not verified. Only the CI-workflow floor remains"}),
)
FEATURE_BY_NAME: Mapping[str, Feature] = MappingProxyType({f.name: f for f in FEATURES})

GATE_CHECK_GUARANTEES: Mapping[str, Tuple[str, str]] = MappingProxyType({
    "vnx_gate_door": ("vnx-gate/door", "the server does not require a green vnx-gate/door: "
                                       "a merge outside the door is not refused"),
    "vnx_gate_review": ("vnx-gate/review", "the server does not require a green vnx-gate/review: "
                                           "a merge without a review verdict is not refused"),
})


@dataclass(frozen=True)
class FeatureState:
    feature: str
    on: bool
    value: str
    source: str
    waived: Tuple[Mapping[str, str], ...] = ()
    server_verified: Optional[bool] = None  # branch_protection: only the door sets it (OF-3)
    conflict: Optional[str] = None  # an off request overruled by the canonical CI sources


def _row(feature: str, key: str, value: str, source: str, guarantee: str) -> Mapping[str, str]:
    return MappingProxyType(dict(feature=feature, key=key, value=value, source=source, guarantee=guarantee))


def _resolve(key: str, project_id: Optional[str], resolver) -> Tuple[Optional[str], str]:
    if resolver is None and (strict := config_runtime.strict_resolver()) is not None:
        resolver, project_id = strict[0], project_id if project_id is not None else strict[1]
    return config_registry.resolve_with_source(
        key, project_id, resolver, use_default_resolver=resolver is None, strict_resolver=True)


def _plan_gate_state(project_id: Optional[str], resolver) -> FeatureState:
    raw, source = _resolve(PLAN_GATE_KEY, project_id, resolver)
    if source == "project_config_error":  # strict: a raising resolver lands here, never as "no row"
        raise OptionalFeatureRefused("plan_gate_mode_unreadable", f"{PLAN_GATE_KEY}: config store unreadable")
    feature, value = FEATURE_BY_NAME[PLAN_GATE], raw.strip().lower() if isinstance(raw, str) else None
    if value in feature.on:
        return FeatureState(PLAN_GATE, True, value, source)
    if value in feature.off:
        return FeatureState(PLAN_GATE, False, value, source,
                            (_row(PLAN_GATE, PLAN_GATE_KEY, value, source, feature.off[value]),))
    raise OptionalFeatureRefused("plan_gate_mode_unknown", f"{PLAN_GATE_KEY}={raw!r} (source: {source})")


def _protection_text(project_root: Optional[Path], protection_yaml: Optional[str],
                     protection_source: Optional[str]) -> Tuple[str, str, Optional[str]]:
    """``(path, source, text)``, ``text`` None for no file; ``protection_source`` alone = absent at that ref."""
    if protection_yaml is not None or protection_source is not None:
        if not protection_source:
            raise OptionalFeatureRefused("branch_protection_unreadable", "protection_yaml without its source")
        return protection_source.rsplit("@", 1)[0], protection_source, protection_yaml
    root = Path(project_root) if project_root is not None else gate_profile._default_project_root()
    found = fpd.find_local_protection_yaml(root)
    for relative in fpd.PROTECTION_YAML_SEARCH_PATHS:
        if root / relative == found:
            break
        if (root / relative).is_symlink() or (root / relative).exists():  # present, not a file: never absent
            raise OptionalFeatureRefused("branch_protection_unreadable", f"{relative}@worktree: not a file")
    if found is None:
        return "", "file_absent", None
    relative = found.relative_to(root).as_posix()
    try:
        return relative, f"{relative}@worktree", found.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise OptionalFeatureRefused("branch_protection_unreadable", f"{relative}@worktree: {exc}") from exc


def _protection_state(source: str, text: Optional[str]) -> FeatureState:
    guarantees = FEATURE_BY_NAME[BRANCH_PROTECTION].off
    if text is None:
        return FeatureState(BRANCH_PROTECTION, False, "absent", "file_absent",
                            (_row(BRANCH_PROTECTION, FILE_KEY, "absent", "file_absent", guarantees["absent"]),))
    try:  # the parser refuses an unknown enforcement value as well as an invalid file
        config = fpd.parse_protection_config(text)
    except fpd.ProtectionConfigError as exc:
        raise OptionalFeatureRefused("branch_protection_unreadable", f"{FILE_KEY} (source: {source}): {exc}") from exc
    contexts = {check.context for check in config.checks}
    rows = [_row(BRANCH_PROTECTION, key, "not_required", source, guarantee)
            for key, (context, guarantee) in GATE_CHECK_GUARANTEES.items() if context not in contexts]
    if config.enforcement in guarantees:
        rows.append(_row(BRANCH_PROTECTION, "enforcement", config.enforcement, source,
                         guarantees[config.enforcement]))
    return FeatureState(BRANCH_PROTECTION, not rows, config.enforcement, source, tuple(rows))


def _ci_state(project_id, resolution, resolver, config_path, project_root,
              path: str, text: Optional[str]) -> FeatureState:
    try:  # ci_requirement reads the same protection text this snapshot read
        required, sources = gate_profile.ci_requirement(
            project_id, resolution=resolution, resolver=resolver, config_path=config_path, project_root=project_root,
            protection_yaml=(path, fpd.YamlFetchResult(text=text, not_found=text is None, error=None)))
    except ReviewGateConfigError as exc:
        raise OptionalFeatureRefused("ci_requirement_unreadable", str(exc)) from exc
    # ci_requirement decided; this read names the request, never changes the requirement.
    raw, source = _resolve(gate_profile.CI_GATE_KEY, project_id, resolver)
    if source == "project_config_error" or not isinstance(raw, str) or raw.strip().lower() not in (
        _FALSY | gate_profile._TRUTHY
    ):
        raise OptionalFeatureRefused("ci_requirement_unreadable", f"{gate_profile.CI_GATE_KEY}={raw!r} (source: {source})")
    if required:
        conflict = (f"{gate_profile.CI_GATE_KEY}={raw.strip()} (source: {source}) has no effect: "
                    + ",".join(sources)) if raw.strip().lower() in _FALSY else None
        return FeatureState(CI, True, "required", ",".join(sources), conflict=conflict)
    if raw.strip().lower() not in _FALSY:
        raise OptionalFeatureRefused(
            "ci_requirement_unreadable", f"{gate_profile.CI_GATE_KEY}={raw!r} (source: {source}) yet not required")
    value = raw.strip()
    return FeatureState(CI, False, value, source,
                        (_row(CI, gate_profile.CI_GATE_KEY, value, source, FEATURE_BY_NAME[CI].off["0"]),))


def snapshot(project_id: Optional[str], *, resolution=None, protection_yaml: Optional[str] = None,
             protection_source: Optional[str] = None, resolver=None, config_path: Optional[Path] = None,
             project_root: Optional[Path] = None) -> Mapping[str, FeatureState]:
    """The state per feature with its source, taken once. ``protection_yaml`` is text fetched from a
    pinned main, ``protection_source`` its ``<path>@<sha>``; else the local file, ``<path>@worktree``.
    That one text also feeds ``ci_requirement``. Raises :class:`OptionalFeatureRefused`."""
    if resolver is None:
        config_runtime.autowire()
    path, source, text = _protection_text(project_root, protection_yaml, protection_source)
    return MappingProxyType({BRANCH_PROTECTION: _protection_state(source, text),
                             CI: _ci_state(project_id, resolution, resolver, config_path, project_root, path, text),
                             PLAN_GATE: _plan_gate_state(project_id, resolver)})


def waived_guarantees(project_id: Optional[str], *, snapshot: Optional[Mapping[str, FeatureState]] = None,
                      **snapshot_kwargs) -> List[Dict[str, str]]:
    """The list from one snapshot (taken here when none is passed), ordered by feature then key."""
    taken = snapshot if snapshot is not None else globals()["snapshot"](project_id, **snapshot_kwargs)
    return sorted((dict(row) for state in taken.values() for row in state.waived),
                  key=lambda row: (row["feature"], row["key"]))
