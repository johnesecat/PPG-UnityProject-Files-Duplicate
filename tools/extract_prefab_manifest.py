#!/usr/bin/env python3
"""Extract a reviewable structural manifest from Unity text prefabs."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

HEADER = re.compile(r"^--- !u!(\d+) &(-?\d+)\s*$")
FIELD = re.compile(r"^  ([A-Za-z0-9_]+):(?:\s*(.*))?$")
FILE_ID = re.compile(r"fileID:\s*(-?\d+)")
GUID = re.compile(r"guid:\s*([0-9a-fA-F]{32})")
CLASS_NAMES = {
    1: "GameObject",
    4: "Transform",
    20: "Camera",
    50: "Rigidbody2D",
    58: "CircleCollider2D",
    60: "PolygonCollider2D",
    61: "BoxCollider2D",
    68: "EdgeCollider2D",
    70: "CapsuleCollider2D",
    114: "MonoBehaviour",
    212: "SpriteRenderer",
    224: "RectTransform",
    231: "DistanceJoint2D",
    232: "FixedJoint2D",
    233: "FrictionJoint2D",
    234: "HingeJoint2D",
    235: "RelativeJoint2D",
    236: "SliderJoint2D",
    237: "SpringJoint2D",
    1001: "PrefabInstance",
}
BOOLEAN_FIELDS = {"m_IsActive", "m_Enabled", "m_IsTrigger", "m_FlipX", "m_FlipY", "m_UseLimits", "m_UseMotor"}


def scalar(value: str):
    value = value.strip()
    if not value:
        return ""
    if value in {"0", "1"}:
        return int(value)
    if value.startswith("'") and value.endswith("'"):
        return value[1:-1].replace("''", "'")
    if value.startswith('"') and value.endswith('"'):
        return value[1:-1]
    try:
        return int(value)
    except ValueError:
        try:
            return float(value)
        except ValueError:
            return value


def inline_value(value: str):
    value = value.strip()
    if value.startswith("{") and value.endswith("}"):
        result = {}
        for match in re.finditer(r"([A-Za-z0-9_]+):\s*([^,}]+)", value[1:-1]):
            key, raw = match.groups()
            parsed = scalar(raw)
            if key == "fileID":
                parsed = str(parsed)
            result[key] = parsed
        return result
    return scalar(value)


def parse_documents(text: str):
    documents = []
    current = None
    for line in text.splitlines():
        header = HEADER.match(line)
        if header:
            if current is not None:
                documents.append(current)
            current = {
                "classId": int(header.group(1)),
                "fileId": header.group(2),
                "lines": [],
            }
        elif current is not None:
            current["lines"].append(line)
    if current is not None:
        documents.append(current)
    return documents


def parse_fields(lines: list[str]):
    fields = {}
    index = 0
    while index < len(lines):
        match = FIELD.match(lines[index])
        if not match:
            index += 1
            continue
        key, raw_value = match.groups()
        raw_value = raw_value or ""
        if raw_value.strip():
            fields[key] = inline_value(raw_value)
            index += 1
            continue

        block = []
        cursor = index + 1
        while cursor < len(lines) and not FIELD.match(lines[cursor]):
            block.append(lines[cursor])
            cursor += 1
        if block:
            fields[key] = "\n".join(line.rstrip() for line in block).strip()
        else:
            fields[key] = ""
        index = cursor
    return fields


def reference_id(value):
    if isinstance(value, dict) and "fileID" in value:
        return str(value["fileID"])
    return None


def parse_component_references(lines: list[str]) -> list[str]:
    references = []
    in_component_list = False
    for line in lines:
        if re.match(r"^  m_Component:\s*$", line):
            in_component_list = True
            continue
        if in_component_list:
            if FIELD.match(line):
                break
            match = re.match(r"^\s*- component:\s*\{fileID:\s*(-?\d+)", line)
            if match:
                references.append(match.group(1))
    return references


def parse_children(lines: list[str]) -> list[str]:
    children = []
    in_children = False
    for line in lines:
        if re.match(r"^  m_Children:\s*$", line):
            in_children = True
            continue
        if in_children:
            if FIELD.match(line):
                break
            match = re.match(r"^\s*- \{fileID:\s*(-?\d+)", line)
            if match:
                children.append(match.group(1))
    return children


def read_guid(path: Path) -> str | None:
    meta = Path(str(path) + ".meta")
    if not meta.is_file():
        return None
    match = re.search(r"(?m)^\s*guid:\s*([0-9a-fA-F]{32})\s*$", meta.read_text(encoding="utf-8", errors="replace"))
    return match.group(1).lower() if match else None


def read_script_names(asset_root: Path) -> dict[str, str]:
    scripts = {}
    for meta in asset_root.rglob("*.cs.meta"):
        match = re.search(r"(?m)^\s*guid:\s*([0-9a-fA-F]{32})\s*$", meta.read_text(encoding="utf-8", errors="replace"))
        if match:
            script_path = Path(str(meta)[:-5])
            scripts[match.group(1).lower()] = script_path.stem
    return scripts


def extract_prefab(path: Path, asset_root: Path, script_names: dict[str, str]) -> dict:
    documents = parse_documents(path.read_text(encoding="utf-8", errors="replace"))
    game_objects = {}
    transforms = {}
    components = {}
    prefab_sources = []

    for document in documents:
        class_id = document["classId"]
        fields = parse_fields(document["lines"])
        if class_id == 1:
            game_objects[document["fileId"]] = {
                "fileId": document["fileId"],
                "name": fields.get("m_Name", f"GameObject {document['fileId']}"),
                "active": bool(fields.get("m_IsActive", 1)),
                "tag": fields.get("m_TagString", "Untagged"),
                "layer": fields.get("m_Layer", 0),
                "componentFileIds": parse_component_references(document["lines"]),
                "transform": None,
                "components": [],
            }
        elif class_id in (4, 224):
            game_object_id = reference_id(fields.get("m_GameObject"))
            if not game_object_id:
                continue
            transform = {
                "fileId": document["fileId"],
                "kind": "RectTransform" if class_id == 224 else "Transform",
                "localPosition": fields.get("m_LocalPosition"),
                "localRotation": fields.get("m_LocalRotation"),
                "localScale": fields.get("m_LocalScale"),
                "parentTransformFileId": reference_id(fields.get("m_Father")),
                "childTransformFileIds": parse_children(document["lines"]),
            }
            if class_id == 224:
                for key in ("m_AnchorMin", "m_AnchorMax", "m_AnchoredPosition", "m_SizeDelta", "m_Pivot"):
                    transform[key] = fields.get(key)
            transforms[document["fileId"]] = {"gameObjectFileId": game_object_id, "data": transform}
        elif class_id == 1001:
            source = fields.get("m_SourcePrefab")
            if source:
                prefab_sources.append(source)
        else:
            game_object_id = reference_id(fields.get("m_GameObject"))
            if not game_object_id:
                continue
            script_guid = None
            script_name = CLASS_NAMES.get(class_id, f"UnityClass{class_id}")
            if class_id == 114:
                script_ref = fields.get("m_Script")
                if isinstance(script_ref, dict):
                    script_guid = str(script_ref.get("guid", "")).lower() or None
                    script_name = script_names.get(script_guid, "MonoBehaviour") if script_guid else "MonoBehaviour"
            components[document["fileId"]] = {
                "fileId": document["fileId"],
                "classId": class_id,
                "type": script_name,
                "scriptGuid": script_guid,
                "fields": fields,
            }

    transform_to_object = {transform_id: record["gameObjectFileId"] for transform_id, record in transforms.items()}
    for transform_id, record in transforms.items():
        game_object = game_objects.get(record["gameObjectFileId"])
        if game_object:
            transform = record["data"]
            parent_id = transform["parentTransformFileId"]
            transform["parentGameObjectFileId"] = transform_to_object.get(parent_id) if parent_id else None
            game_object["transform"] = transform

    for game_object in game_objects.values():
        for component_id in game_object["componentFileIds"]:
            component = components.get(component_id)
            if component:
                game_object["components"].append(component)

    roots = [
        game_object_id
        for game_object_id, game_object in game_objects.items()
        if not game_object["transform"] or not game_object["transform"]["parentGameObjectFileId"]
    ]
    return {
        "sourcePath": path.relative_to(asset_root).as_posix(),
        "assetGuid": read_guid(path),
        "sourcePrefabReferences": prefab_sources,
        "rootGameObjectFileIds": roots,
        "gameObjects": sorted(game_objects.values(), key=lambda item: int(item["fileId"])),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assets", type=Path, default=Path("ppg/People Playground/Assets"), help="Unity Assets directory")
    parser.add_argument("--output", type=Path, default=Path(".agent/generated/prefab_manifest.json"), help="Review manifest output")
    parser.add_argument("--max-file-mb", type=int, default=64, help="Skip larger text prefabs")
    arguments = parser.parse_args()

    asset_root = arguments.assets.resolve()
    if not asset_root.is_dir():
        parser.error(f"Assets directory does not exist: {asset_root}")
    script_names = read_script_names(asset_root)
    prefabs = []
    skipped = []
    for path in sorted(asset_root.rglob("*.prefab")):
        if path.stat().st_size > arguments.max_file_mb * 1024 * 1024:
            skipped.append(path.relative_to(asset_root).as_posix())
            continue
        try:
            prefabs.append(extract_prefab(path, asset_root, script_names))
        except (OSError, ValueError) as error:
            skipped.append(f"{path.relative_to(asset_root).as_posix()}: {error}")

    manifest = {
        "assetRoot": asset_root.as_posix(),
        "prefabCount": len(prefabs),
        "scriptGuidCount": len(script_names),
        "skipped": skipped,
        "prefabs": prefabs,
    }
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Extracted {len(prefabs)} prefabs; skipped {len(skipped)}. Wrote {arguments.output}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
