#!/usr/bin/env python3
"""Create a reviewable Unity source-data bundle for the People Playground Luau port.

This is an offline analysis/export tool intended to run in Google Colab or locally.
It never uploads assets and never reads credentials. It emits manifests, small lazy
Luau pixel modules, prefab/scene structure, script indexes, and raw .byte/.bytes files.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import importlib.util
import json
import re
import shutil
import sys
import zipfile
from pathlib import Path

SKIP_DIRS = {".git", "Library", "Temp", "obj", "bin", "Build", "Builds", "Logs"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".tga", ".psd", ".tif", ".tiff", ".bmp", ".webp", ".gif", ".exr", ".hdr"}
TEXT_ASSET_EXTENSIONS = {
    ".prefab", ".unity", ".asset", ".mat", ".controller", ".anim",
    ".overridecontroller", ".mask", ".playable", ".rendertexture", ".guiskin",
}
GUID_RE = re.compile(r"(?m)^\s*guid:\s*([0-9a-fA-F]{32})\s*$")
REFERENCE_RE = re.compile(r"guid:\s*([0-9a-fA-F]{32})")
CLASS_RE = re.compile(r"\b(?:class|struct|interface|enum)\s+(\w+)(?:\s*:\s*([^\{\n]+))?")
METHOD_RE = re.compile(
    r"(?m)^\s*(?:(?:public|private|protected|internal|static|virtual|override|async|new|sealed|abstract|extern|partial|unsafe)\s+)+"
    r"[\w<>,\[\]\.?]+\s+(\w+)\s*\(([^;{}\n]*)\)\s*(?:where\s+[^\{]+)?(?:\{|=>|;)"
)


def safe_relative(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def load_local_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load project tool: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def iter_files(root: Path):
    for path in root.rglob("*"):
        if not path.is_file() or any(part in SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        yield path


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def classify(path: Path) -> str:
    extension = path.suffix.lower()
    if extension in IMAGE_EXTENSIONS:
        return "image"
    if extension in {".cs", ".dll", ".pdb", ".asmdef", ".asmref"}:
        return "code"
    if extension in {".byte", ".bytes"}:
        return "byte-payload"
    if extension == ".prefab":
        return "prefab"
    if extension == ".unity":
        return "scene"
    if extension in {".mat", ".asset", ".shader", ".shadervariants"}:
        return "render-or-data"
    return "other"


def importer_settings(meta_text: str) -> dict:
    keys = (
        "textureType", "spriteMode", "spritePixelsToUnits", "maxTextureSize",
        "filterMode", "aniso", "mipmaps", "sRGBTexture", "alphaIsTransparency",
        "compressionQuality", "textureCompression", "isReadable", "wrapMode",
    )
    settings = {}
    for key in keys:
        match = re.search(rf"(?m)^\s*{re.escape(key)}:\s*([^\s#]+)", meta_text)
        if match:
            settings[key] = match.group(1)
    return settings


def parse_inline_mapping(value: str) -> dict:
    result = {}
    for match in re.finditer(r"([A-Za-z0-9_]+):\s*([^,}]+)", value.strip().strip("{}")):
        key, raw = match.groups()
        raw = raw.strip().strip("'").strip('"')
        try:
            result[key] = float(raw) if any(character in raw for character in ".eE") else int(raw)
        except ValueError:
            result[key] = raw
    return result


def parse_sprite_sheet(meta_text: str) -> list[dict]:
    lines = meta_text.splitlines()
    sprites_line = next((index for index, line in enumerate(lines) if re.match(r"^\s*sprites:\s*$", line)), None)
    if sprites_line is None:
        return []
    base_indent = len(lines[sprites_line]) - len(lines[sprites_line].lstrip())
    sprites = []
    current = None
    for line in lines[sprites_line + 1:]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if indent <= base_indent:
            break
        if re.match(r"^\s*-\s*", line):
            current = {}
            sprites.append(current)
            stripped = re.sub(r"^\s*-\s*", "", line)
            if not stripped:
                continue
        if current is None or ":" not in stripped:
            continue
        key, raw = stripped.split(":", 1)
        raw = raw.strip()
        if raw.startswith("{") and raw.endswith("}"):
            current[key] = parse_inline_mapping(raw)
        elif raw:
            current[key] = raw.strip("'").strip('"')
    return sprites


def scan_asset_metas(asset_root: Path):
    entries = []
    guids_to_paths = collections.defaultdict(list)
    meta_paths = set()
    missing_guid = []
    missing_asset = []
    for meta_path in sorted(asset_root.rglob("*.meta")):
        if any(part in SKIP_DIRS for part in meta_path.relative_to(asset_root).parts):
            continue
        meta_paths.add(Path(str(meta_path)[:-5]))
        try:
            text = meta_path.read_text(encoding="utf-8", errors="replace")
        except OSError as error:
            missing_guid.append({"meta": safe_relative(meta_path, asset_root), "error": str(error)})
            continue
        match = GUID_RE.search(text)
        if not match:
            missing_guid.append({"meta": safe_relative(meta_path, asset_root), "error": "no 32-character GUID"})
            continue
        guid = match.group(1).lower()
        asset_path = Path(str(meta_path)[:-5])
        exists = asset_path.exists()
        if not exists:
            missing_asset.append({"guid": guid, "path": safe_relative(asset_path, asset_root)})
        guids_to_paths[guid].append(safe_relative(asset_path, asset_root))
        entries.append({
            "guid": guid,
            "path": safe_relative(asset_path, asset_root),
            "kind": classify(asset_path),
            "extension": asset_path.suffix.lower(),
            "fileBacked": exists,
            "fileSize": asset_path.stat().st_size if asset_path.is_file() else 0,
            "importerSettings": importer_settings(text),
        })

    files_without_meta = []
    for path in iter_files(asset_root):
        if path.suffix.lower() == ".meta":
            continue
        if path not in meta_paths and not Path(str(path) + ".meta").is_file():
            files_without_meta.append(safe_relative(path, asset_root))

    duplicates = [
        {"guid": guid, "paths": paths}
        for guid, paths in sorted(guids_to_paths.items())
        if len(paths) > 1
    ]
    return entries, dict(guids_to_paths), {
        "metaCount": len(meta_paths),
        "assetsWithGuid": len(entries),
        "missingGuidMeta": missing_guid,
        "metaWithoutAsset": missing_asset,
        "filesWithoutAdjacentMeta": files_without_meta,
        "duplicateGuids": duplicates,
    }


def scan_guid_references(asset_root: Path, guids_to_paths: dict):
    references = collections.defaultdict(list)
    scanned_files = 0
    skipped_large = []
    for path in iter_files(asset_root):
        if path.suffix.lower() not in TEXT_ASSET_EXTENSIONS:
            continue
        try:
            if path.stat().st_size > 32 * 1024 * 1024:
                skipped_large.append(safe_relative(path, asset_root))
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        scanned_files += 1
        for match in REFERENCE_RE.finditer(text):
            guid = match.group(1).lower()
            if guid == "0" * 32:
                continue
            references[guid].append(safe_relative(path, asset_root))
    unresolved = [
        {"guid": guid, "referencedBy": sorted(set(paths))}
        for guid, paths in sorted(references.items())
        if guid not in guids_to_paths
    ]
    return {
        "scannedTextFiles": scanned_files,
        "guidReferenceCount": sum(len(paths) for paths in references.values()),
        "uniqueReferencedGuids": len(references),
        "referencesByGuid": {guid: sorted(set(paths)) for guid, paths in sorted(references.items())},
        "unresolvedGuidReferences": unresolved,
        "largeFilesSkipped": skipped_large,
    }


def scan_source_code(asset_root: Path, output_root: Path):
    files = []
    copied_root = output_root / "source_code"
    code_paths = sorted(asset_root.rglob("*.cs"))
    keyword_groups = {
        "characters-and-health": re.compile(r"person|limb|circulation|connectednode|ragdoll|pose|health|skin", re.I),
        "physics-and-wires": re.compile(r"joint|rigidbody|wire|hinge|distance|spring|slider|collision|force", re.I),
        "weapons-and-effects": re.compile(r"gun|weapon|bullet|projectile|explosion|fire|electric|chem|syringe|vehicle|machine", re.I),
    }
    systems = {name: [] for name in keyword_groups}
    for path in code_paths:
        if any(part in SKIP_DIRS for part in path.relative_to(asset_root).parts):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        source_copy = copied_root / path.relative_to(asset_root)
        source_copy.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, source_copy)
        classes = [
            {"name": match.group(1), "inherits": (match.group(2) or "").strip(), "line": text.count("\n", 0, match.start()) + 1}
            for match in CLASS_RE.finditer(text)
        ]
        methods = [
            {"name": match.group(1), "parameters": match.group(2).strip(), "line": text.count("\n", 0, match.start()) + 1}
            for match in METHOD_RE.finditer(text)
        ]
        script_meta = Path(str(path) + ".meta")
        script_meta_text = script_meta.read_text(encoding="utf-8", errors="replace") if script_meta.is_file() else ""
        script_guid_match = GUID_RE.search(script_meta_text)
        record = {
            "path": safe_relative(path, asset_root),
            "sourceCopy": safe_relative(source_copy, output_root),
            "scriptGuid": script_guid_match.group(1).lower() if script_guid_match else None,
            "sha256": hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest(),
            "lines": text.count("\n") + 1,
            "classes": classes,
            "methods": methods,
        }
        files.append(record)
        searchable = path.name + " " + " ".join(item["name"] for item in classes)
        for group, pattern in keyword_groups.items():
            if pattern.search(searchable):
                systems[group].append(record["path"])
    script_guid_to_paths = collections.defaultdict(list)
    for record in files:
        if record["scriptGuid"]:
            script_guid_to_paths[record["scriptGuid"]].append(record["path"])
    return {
        "csharpFileCount": len(files),
        "files": files,
        "scriptGuidToPaths": {guid: sorted(paths) for guid, paths in sorted(script_guid_to_paths.items())},
        "systemFileGroups": systems,
        "managedBinaries": [
            {"path": safe_relative(path, asset_root), "fileSize": path.stat().st_size, "sha256": sha256_file(path)}
            for path in iter_files(asset_root) if path.suffix.lower() in {".dll", ".pdb"}
        ],
    }


def export_texture_pixels(asset_root: Path, output_root: Path, max_dimension: int, max_source_pixels: int, copy_original: bool = False):
    try:
        from PIL import Image
    except ImportError:
        return {"textureCount": 0, "pixelModules": 0, "originalTextureFilesCopied": 0, "importerMetaFilesCopied": 0, "errors": [{"error": "Pillow is unavailable; install Pillow in Colab and rerun."}], "files": []}

    texture_root = asset_root / "Texture2D"
    if not texture_root.is_dir():
        match = next((child for child in asset_root.iterdir() if child.is_dir() and child.name.lower() == "texture2d"), None)
        texture_root = match or texture_root
    if not texture_root.is_dir():
        return {"textureRoot": "Texture2D", "textureCount": 0, "pixelModules": 0, "originalTextureFilesCopied": 0, "importerMetaFilesCopied": 0, "errors": [{"error": "Texture2D directory not found"}], "files": []}

    tools_root = Path(__file__).resolve().parent
    encoder = load_local_module("asset_index_encoder", tools_root / "build_asset_index.py")
    pixel_root = output_root / "pixel_modules"
    pixel_root.mkdir(parents=True, exist_ok=True)
    manifest = []
    error_list = []
    module_count = 0
    used_module_names = set()
    paths = sorted(
        path for path in texture_root.rglob("*")
        if path.is_file()
        and path.suffix.lower() in IMAGE_EXTENSIONS
        and not any(part in SKIP_DIRS for part in path.relative_to(texture_root).parts)
    )

    for path in paths:
        relative_path = safe_relative(path, asset_root)
        meta_path = Path(str(path) + ".meta")
        meta_text = meta_path.read_text(encoding="utf-8", errors="replace") if meta_path.is_file() else ""
        guid_match = GUID_RE.search(meta_text)
        guid = guid_match.group(1).lower() if guid_match else None
        importer_meta_copy = None
        if meta_text:
            meta_key = guid or hashlib.sha256(relative_path.encode()).hexdigest()[:16]
            meta_suffix = hashlib.sha256(relative_path.encode()).hexdigest()[:8]
            importer_copy_path = output_root / "texture_importer_meta" / f"{meta_key}__{meta_suffix}.meta"
            importer_copy_path.parent.mkdir(parents=True, exist_ok=True)
            importer_copy_path.write_text(meta_text, encoding="utf-8")
            importer_meta_copy = safe_relative(importer_copy_path, output_root)
        original_copy = None
        if copy_original:
            key = guid or hashlib.sha256(relative_path.encode()).hexdigest()[:16]
            suffix = hashlib.sha256(relative_path.encode()).hexdigest()[:8]
            original_path = output_root / "source_textures" / f"{key}__{suffix}__{path.name}"
            original_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, original_path)
            original_copy = safe_relative(original_path, output_root)
        source_width = source_height = None
        image_mode = image_format = None
        module_name = None
        module_size = 0
        error = None
        try:
            with Image.open(path) as source:
                source_width, source_height = source.size
                image_mode = source.mode
                image_format = source.format
                if source_width * source_height > max_source_pixels:
                    raise ValueError(f"source exceeds max-source-pixels ({max_source_pixels})")
                source.seek(0)
                image = source.convert("RGBA")
                if max_dimension > 0 and max(image.size) > max_dimension:
                    scale = max_dimension / max(image.size)
                    target_size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
                    resampling = getattr(Image, "Resampling", Image)
                    image = image.resize(target_size, resampling.NEAREST)
                width, height = image.size
                rgba = bytearray(image.tobytes())
                encoded = encoder.encode_runs(rgba)
                if len(encoded) < len(rgba):
                    encoding, payload = "rle", encoded
                else:
                    encoding, payload = "rgba", bytes(rgba)
                key = guid or ("missing-" + hashlib.sha256(path.as_posix().encode()).hexdigest()[:16])
                module_name = key
                if module_name in used_module_names:
                    module_name += "__" + hashlib.sha256(path.as_posix().encode()).hexdigest()[:8]
                used_module_names.add(module_name)
                module_text = encoder.emit_sprite_module(width, height, encoding, payload)
                module_path = pixel_root / f"{module_name}.lua"
                module_path.write_text(module_text, encoding="utf-8")
                module_size = module_path.stat().st_size
                module_count += 1
        except Exception as error_value:
            error = f"{type(error_value).__name__}: {error_value}"
            module_name = None
            module_size = 0
            error_list.append({"path": relative_path, "guid": guid, "error": error})

        manifest.append({
            "guid": guid,
            "path": relative_path,
            "fileSize": path.stat().st_size,
            "sha256": sha256_file(path),
            "width": source_width,
            "height": source_height,
            "mode": image_mode,
            "format": image_format,
            "sourceCopy": original_copy,
            "importerMetaCopy": importer_meta_copy,
            "spriteImporterSettings": importer_settings(meta_text),
            "spriteSubassets": parse_sprite_sheet(meta_text),
            "pixelModule": f"pixel_modules/{module_name}.lua" if module_name else None,
            "pixelModuleSizeBytes": module_size,
            "pixelError": error,
        })

    return {
        "textureRoot": safe_relative(texture_root, asset_root),
        "textureCount": len(paths),
        "pixelModules": module_count,
        "originalTextureFilesCopied": sum(1 for item in manifest if item["sourceCopy"]),
        "importerMetaFilesCopied": sum(1 for item in manifest if item["importerMetaCopy"]),
        "maxImageDimension": max_dimension,
        "maxSourcePixels": max_source_pixels,
        "errors": error_list,
        "files": manifest,
    }


def export_byte_payloads(asset_root: Path, output_root: Path, max_file_mb: int):
    payload_root = output_root / "byte_payloads"
    entries = []
    skipped = []
    total_bytes = 0
    for path in iter_files(asset_root):
        if path.suffix.lower() not in {".byte", ".bytes"}:
            continue
        relative = safe_relative(path, asset_root)
        meta_path = Path(str(path) + ".meta")
        meta_text = meta_path.read_text(encoding="utf-8", errors="replace") if meta_path.is_file() else ""
        match = GUID_RE.search(meta_text)
        guid = match.group(1).lower() if match else None
        size = path.stat().st_size
        if size > max_file_mb * 1024 * 1024:
            skipped.append({"path": relative, "fileSize": size, "reason": f"larger than {max_file_mb} MB limit"})
            continue
        key = guid or hashlib.sha256(path.as_posix().encode()).hexdigest()[:16]
        suffix = hashlib.sha256(relative.encode()).hexdigest()[:8]
        destination = payload_root / f"{key}__{suffix}__{path.name}"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(path.read_bytes())
        total_bytes += size
        entries.append({
            "guid": guid,
            "sourcePath": relative,
            "exportPath": safe_relative(destination, output_root),
            "fileSize": size,
            "sha256": sha256_file(path),
        })
    return {"payloadCount": len(entries), "totalBytes": total_bytes, "skipped": skipped, "files": entries}


def export_prefabs_and_scenes(asset_root: Path, output_root: Path, max_file_mb: int, scene_limit: int):
    extractor_path = Path(__file__).resolve().parent / "extract_prefab_manifest.py"
    extractor = load_local_module("prefab_manifest_extractor", extractor_path)
    script_names = extractor.read_script_names(asset_root)
    max_bytes = max_file_mb * 1024 * 1024
    prefabs = []
    skipped_prefabs = []
    for path in sorted(asset_root.rglob("*.prefab")):
        if any(part in SKIP_DIRS for part in path.relative_to(asset_root).parts):
            continue
        if path.stat().st_size > max_bytes:
            skipped_prefabs.append({"path": safe_relative(path, asset_root), "reason": f"larger than {max_file_mb} MB"})
            continue
        try:
            prefabs.append(extractor.extract_prefab(path, asset_root, script_names))
        except Exception as error:
            skipped_prefabs.append({"path": safe_relative(path, asset_root), "reason": f"{type(error).__name__}: {error}"})

    scene_paths = [
        path for path in asset_root.rglob("*.unity")
        if not any(part in SKIP_DIRS for part in path.relative_to(asset_root).parts)
    ]
    scene_paths.sort(key=lambda path: (path.stem.lower() not in {"menu", "main"}, path.as_posix().lower()))
    scenes = []
    skipped_scenes = []
    for path in scene_paths[:scene_limit]:
        if path.stat().st_size > max_bytes:
            skipped_scenes.append({"path": safe_relative(path, asset_root), "reason": f"larger than {max_file_mb} MB"})
            continue
        try:
            scenes.append(extractor.extract_prefab(path, asset_root, script_names))
        except Exception as error:
            skipped_scenes.append({"path": safe_relative(path, asset_root), "reason": f"{type(error).__name__}: {error}"})
    for path in scene_paths[scene_limit:]:
        skipped_scenes.append({"path": safe_relative(path, asset_root), "reason": "scene limit; raise --scene-limit to include"})

    def component_counts(records):
        counts = collections.Counter()
        for record in records:
            for game_object in record.get("gameObjects", []):
                for component in game_object.get("components", []):
                    counts[component.get("type", "unknown")] += 1
        return dict(sorted(counts.items()))

    return {
        "scriptGuidCount": len(script_names),
        "scriptGuidToName": dict(sorted(script_names.items())),
        "prefabs": {"count": len(prefabs), "skipped": skipped_prefabs, "componentCounts": component_counts(prefabs), "records": prefabs},
        "scenes": {"discovered": len(scene_paths), "extracted": len(scenes), "skipped": skipped_scenes, "componentCounts": component_counts(scenes), "records": scenes},
    }


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assets", type=Path, default=Path("ppg/People Playground/Assets"), help="Unity Assets directory")
    parser.add_argument("--output", type=Path, default=Path("/content/ppg-source-export"), help="Export directory")
    parser.add_argument("--max-image-dimension", type=int, default=96, help="Nearest-neighbor pixel module cap; 0 preserves original resolution")
    parser.add_argument("--max-source-pixels", type=int, default=16_777_216, help="Skip decoding source images larger than this pixel count")
    parser.add_argument("--max-file-mb", type=int, default=64, help="Skip oversized YAML and byte-payload files")
    parser.add_argument("--scene-limit", type=int, default=8, help="Maximum scene YAML files to parse; Menu and Main are prioritized")
    parser.add_argument("--copy-textures", action="store_true", help="Copy original Texture2D files into the export; may create a large ZIP")
    parser.add_argument("--zip", action="store_true", help="Create a ZIP next to the export directory")
    arguments = parser.parse_args()

    asset_root = arguments.assets.resolve()
    output_root = arguments.output.resolve()
    if not asset_root.is_dir():
        parser.error(f"Assets directory not found: {asset_root}")
    if arguments.max_image_dimension < 0 or arguments.max_source_pixels < 1 or arguments.max_file_mb < 1 or arguments.scene_limit < 0:
        parser.error("size limits must be non-negative, and source/file limits must be positive")

    output_root.mkdir(parents=True, exist_ok=True)
    asset_entries, guid_to_paths, asset_stats = scan_asset_metas(asset_root)
    reference_stats = scan_guid_references(asset_root, guid_to_paths)
    source_code = scan_source_code(asset_root, output_root)
    textures = export_texture_pixels(
        asset_root, output_root, arguments.max_image_dimension,
        arguments.max_source_pixels, arguments.copy_textures,
    )
    byte_payloads = export_byte_payloads(asset_root, output_root, arguments.max_file_mb)
    structure = export_prefabs_and_scenes(asset_root, output_root, arguments.max_file_mb, arguments.scene_limit)

    write_json(output_root / "asset_index.json", {"assetRoot": asset_root.as_posix(), "stats": asset_stats, "assets": asset_entries})
    write_json(output_root / "guid_references.json", reference_stats)
    write_json(output_root / "texture_inventory.json", textures)
    write_json(output_root / "source_code_index.json", source_code)
    write_json(output_root / "byte_payload_inventory.json", byte_payloads)
    write_json(output_root / "prefab_scene_manifest.json", structure)

    component_counts = structure["prefabs"]["componentCounts"]
    report = [
        "# Unity source export summary", "",
        f"- Unity Assets root: `{asset_root}`",
        f"- `.meta` files scanned: {asset_stats['metaCount']}",
        f"- GUID-indexed entries: {asset_stats['assetsWithGuid']}",
        f"- Duplicate GUID groups: {len(asset_stats['duplicateGuids'])}",
        f"- `.meta` files without GUIDs: {len(asset_stats['missingGuidMeta'])}",
        f"- `.meta` entries whose source file/folder is missing: {len(asset_stats['metaWithoutAsset'])}",
        f"- Source files without an adjacent `.meta`: {len(asset_stats['filesWithoutAdjacentMeta'])}",
        f"- Unresolved serialized GUID references: {len(reference_stats['unresolvedGuidReferences'])}",
        f"- Texture2D images: {textures['textureCount']}; pixel modules: {textures['pixelModules']}; importer `.meta` copies: {textures['importerMetaFilesCopied']}; original image copies: {textures['originalTextureFilesCopied']}; decode errors: {len(textures['errors'])}",
        f"- Prefabs extracted: {structure['prefabs']['count']}; prefabs skipped: {len(structure['prefabs']['skipped'])}",
        f"- Scenes extracted: {structure['scenes']['extracted']} / {structure['scenes']['discovered']}; skipped/not included: {len(structure['scenes']['skipped'])}",
        f"- C# source files indexed and copied unchanged: {source_code['csharpFileCount']}",
        f"- `.byte`/`.bytes` payloads copied: {byte_payloads['payloadCount']} ({byte_payloads['totalBytes']} bytes)",
        "",
        "## Common prefab components",
    ]
    for name, count in sorted(component_counts.items(), key=lambda item: (-item[1], item[0]))[:30]:
        report.append(f"- `{name}`: {count}")
    report.extend([
        "", "## Important limits", "",
        "- Pixel modules are nearest-neighbor downsampled to the configured cap unless it is set to 0. Original texture files are copied only when `--copy-textures` is passed; otherwise the cloned source repository retains them.",
        "- All `.cs` source files are copied unchanged to `source_code/`; method/class indexing is approximate and is not a C# parser.",
        "- Unity sprite-sheet rectangles, pivots, borders and importer settings are indexed, and each Texture2D importer `.meta` is copied verbatim under `texture_importer_meta/`.",
        "- Prefab and scene YAML parsing is structural, not a full Unity deserializer. Prefab-instance overrides, custom serialization, binary scenes, and arbitrary managed/native DLL behavior still need review.",
        "- The C# index identifies classes and method signatures; it does not decompile DLLs or automatically translate C# systems to Luau. `.byte`/`.bytes` payloads are copied unchanged.",
        "- No generated export is committed or pushed by this tool. Review manifests and Roblox module limits before importing output into a place.",
        "",
    ])
    (output_root / "SUMMARY.md").write_text("\n".join(report), encoding="utf-8")

    print(f"Export complete: {output_root}")
    print(f"Assets={asset_stats['metaCount']} textures={textures['textureCount']} prefabs={structure['prefabs']['count']} scenes={structure['scenes']['extracted']} C#={source_code['csharpFileCount']} bytes={byte_payloads['payloadCount']}")
    if arguments.zip:
        zip_path = output_root.with_suffix(".zip")
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for path in sorted(output_root.rglob("*")):
                if path.is_file():
                    archive.write(path, safe_relative(path, output_root))
        print(f"ZIP: {zip_path} ({zip_path.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
