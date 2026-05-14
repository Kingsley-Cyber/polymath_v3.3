"""Roblox/Luau entity type resolver — scoped to Luau code chunks.

Phase 5 Gate 1 — replaces the hardcoded entity_type="Method" / "Artifact"
in `worker._synthesize_code_extraction_results` for known Roblox engine
terms, ONLY when the chunk's context strongly suggests this is a
Roblox/Luau codebase.

Scope gate: returns a Roblox-specific type only when EITHER:
  - chunk.language is "lua" or "luau", OR
  - chunk.metadata["roblox_apis"] is non-empty (e.g., a code-fence
    inside a markdown doc where the language tag is empty but the
    extractor already flagged Roblox patterns).

Other chunks (prose, Python, JS, ambiguous text) get None and fall
through to the worker's default Method/Artifact assignment.

Why scoped: a GLOBAL override (e.g. adding `"Spring": "Class"` to
entity_type_overrides.json) would re-type:
  - a book chapter about spring weather → "Class"
  - a Python `spring = requests.get(...)` variable → "Class"
  - a Java Spring Framework reference → "Class"
The scope check prevents that pollution entirely.

v1 keeps the table small and boring. Generic/ambiguous names
(`Spring`, `Value`, `New`, `Service`, `Controller`, `Component`,
`Promise`, `t`, `string`, `function`, `table`) are DELIBERATELY
EXCLUDED because they collide with non-Roblox semantics even within
Luau corpora. Adding them later requires a contextual gate stronger
than language alone (e.g., file_path == .luau AND chunk text imports
a known Roblox service).
"""

from __future__ import annotations

from typing import Any


# High-confidence Roblox engine terms. Each entry is unambiguous when
# read in the context of Luau code — there is no non-Roblox meaning
# that collides. Sourced from Roblox/creator-docs and rbx-dom reflection.
_ROBLOX_ENTITY_TYPES: dict[str, str] = {
    # ── Services (game:GetService targets) ─────────────────────────────
    "TweenService":         "RobloxService",
    "RunService":           "RobloxService",
    "Players":              "RobloxService",
    "ReplicatedStorage":    "RobloxService",
    "ServerStorage":        "RobloxService",
    "ServerScriptService":  "RobloxService",
    "UserInputService":     "RobloxService",
    "HttpService":          "RobloxService",
    "DataStoreService":     "RobloxService",
    "Lighting":             "RobloxService",
    "Workspace":            "RobloxService",
    "Chat":                 "RobloxService",
    "TextChatService":      "RobloxService",
    "MarketplaceService":   "RobloxService",
    "PhysicsService":       "RobloxService",
    "ContextActionService": "RobloxService",
    "CollectionService":    "RobloxService",
    "PathfindingService":   "RobloxService",
    "Teams":                "RobloxService",
    "SoundService":         "RobloxService",
    "Debris":               "RobloxService",
    # ── Network primitives ─────────────────────────────────────────────
    "RemoteEvent":          "RobloxNetworkPrimitive",
    "RemoteFunction":       "RobloxNetworkPrimitive",
    "BindableEvent":        "RobloxNetworkPrimitive",
    "BindableFunction":     "RobloxNetworkPrimitive",
    "UnreliableRemoteEvent": "RobloxNetworkPrimitive",
    # ── Instance classes ───────────────────────────────────────────────
    "Instance":             "RobloxClass",
    "Part":                 "RobloxClass",
    "MeshPart":             "RobloxClass",
    "Model":                "RobloxClass",
    "Humanoid":             "RobloxClass",
    "HumanoidRootPart":     "RobloxClass",
    "Animation":            "RobloxClass",
    "AnimationTrack":       "RobloxClass",
    "Animator":             "RobloxClass",
    "ParticleEmitter":      "RobloxClass",
    "Beam":                 "RobloxClass",
    "Sound":                "RobloxClass",
    "Attachment":           "RobloxClass",
    "Tool":                 "RobloxClass",
    "ScreenGui":            "RobloxClass",
    "Frame":                "RobloxClass",
    "TextLabel":            "RobloxClass",
    "TextButton":           "RobloxClass",
    "ImageLabel":           "RobloxClass",
    "ImageButton":          "RobloxClass",
    "ModuleScript":         "RobloxClass",
    "LocalScript":          "RobloxClass",
    "Script":               "RobloxClass",
    "Folder":               "RobloxClass",
    # ── Joints, constraints, body movers ──────────────────────────────
    # Added in the "Roblox ontology audit" pass — none of these collide
    # with non-Roblox semantics, and they're high-frequency in animation
    # / physics / rig code so the spider+electric query benefits.
    "Motor6D":              "RobloxClass",
    "Weld":                 "RobloxClass",
    "WeldConstraint":       "RobloxClass",
    "Constraint":           "RobloxClass",
    "AlignOrientation":     "RobloxClass",
    "AlignPosition":        "RobloxClass",
    "AngularVelocity":      "RobloxClass",
    "LinearVelocity":       "RobloxClass",
    "BodyVelocity":         "RobloxClass",
    "BodyAngularVelocity":  "RobloxClass",
    "BodyPosition":         "RobloxClass",
    "BodyGyro":             "RobloxClass",
    "BodyForce":            "RobloxClass",
    "BodyMover":            "RobloxClass",
    "VectorForce":          "RobloxClass",
    "Torque":               "RobloxClass",
    "AnimationController":  "RobloxClass",
    "Hinge":                "RobloxClass",
    "HingeConstraint":      "RobloxClass",
    "BallSocketConstraint": "RobloxClass",
    "SpringConstraint":     "RobloxClass",
    "RopeConstraint":       "RobloxClass",
    "RodConstraint":        "RobloxClass",
    "Trail":                "RobloxClass",
    "PointLight":           "RobloxClass",
    "SpotLight":            "RobloxClass",
    "SurfaceLight":         "RobloxClass",
    "Decal":                "RobloxClass",
    "Texture":              "RobloxClass",
    "ProximityPrompt":      "RobloxClass",
    "ClickDetector":        "RobloxClass",
    # ── Luau data types ────────────────────────────────────────────────
    "Vector3":              "LuauDataType",
    "Vector2":              "LuauDataType",
    "CFrame":               "LuauDataType",
    "Color3":               "LuauDataType",
    "UDim":                 "LuauDataType",
    "UDim2":                "LuauDataType",
    "Ray":                  "LuauDataType",
    "Region3":              "LuauDataType",
    "NumberSequence":       "LuauDataType",
    "ColorSequence":        "LuauDataType",
    "Enum":                 "LuauDataType",
    "EnumItem":             "LuauDataType",
}

# Domain groupings — surfaced as canonical_family hints for retrieval
# and graph decoration. Each `known` member is also in _ROBLOX_ENTITY_TYPES.
_ROBLOX_DOMAINS: dict[str, dict[str, Any]] = {
    "NetworkReplication": {
        "root": "Roblox",
        "known": ["RemoteEvent", "RemoteFunction", "BindableEvent",
                  "BindableFunction", "UnreliableRemoteEvent"],
    },
    "AnimationSystem": {
        "root": "Roblox",
        "known": ["TweenService", "Animation", "AnimationTrack",
                  "Animator", "AnimationController", "Motor6D"],
    },
    "PhysicsSimulation": {
        "root": "Roblox",
        "known": ["Workspace", "Part", "MeshPart", "Humanoid",
                  "HumanoidRootPart", "Vector3", "CFrame", "Region3",
                  "BodyVelocity", "BodyAngularVelocity", "BodyPosition",
                  "BodyGyro", "BodyForce", "VectorForce", "Torque",
                  "LinearVelocity", "AngularVelocity",
                  "AlignPosition", "AlignOrientation",
                  "HingeConstraint", "BallSocketConstraint",
                  "SpringConstraint", "RopeConstraint", "RodConstraint",
                  "WeldConstraint", "Motor6D", "Weld"],
    },
    "UIFramework": {
        "root": "Roblox",
        "known": ["ScreenGui", "Frame", "TextLabel", "TextButton",
                  "ImageLabel", "ImageButton", "UDim", "UDim2"],
    },
    "DataPersistence": {
        "root": "Roblox",
        "known": ["DataStoreService", "HttpService", "MarketplaceService"],
    },
    "InputHandling": {
        "root": "Roblox",
        "known": ["UserInputService", "ContextActionService"],
    },
    "VFXAudio": {
        "root": "Roblox",
        "known": ["ParticleEmitter", "Beam", "Sound", "SoundService",
                  "Attachment", "Lighting"],
    },
}


def resolve_code_entity_type(name: str, chunk: Any) -> str | None:
    """Return a Roblox-specific entity type ONLY when the chunk's
    context strongly suggests this is a Roblox/Luau codebase.

    Returns None when:
      - chunk is not Luau/Lua AND has no roblox_apis metadata
      - name is not in the curated table

    Callers (worker._synthesize_code_extraction_results) treat None as
    "fall through to the default Method/Artifact assignment".
    """
    if not name:
        return None
    lang = (getattr(chunk, "language", None) or "").lower()
    meta = getattr(chunk, "metadata", None) or {}
    is_luau = lang in ("lua", "luau")
    has_roblox_apis = bool(meta.get("roblox_apis"))
    if not is_luau and not has_roblox_apis:
        return None
    return _ROBLOX_ENTITY_TYPES.get(name)


def roblox_domain_for(name: str) -> str | None:
    """Reverse lookup: which Roblox domain (NetworkReplication / etc.)
    does this canonical name belong to? Returns None for unknown names
    OR names that aren't in any domain grouping.

    Used at Phase 4 entity write time to stamp `canonical_family` on
    the Entity node so Mode A decoration can render
    `--uses(NetworkReplication)-->` etc.
    """
    if not name:
        return None
    for domain, payload in _ROBLOX_DOMAINS.items():
        if name in payload.get("known", []):
            return domain
    return None
