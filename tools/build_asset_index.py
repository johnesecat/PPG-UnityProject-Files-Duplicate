#!/usr/bin/env python3
"""Build a GUID-indexed Luau catalog and lazy PNG pixel modules from Unity Assets."""

from __future__ import annotations

import argparse
import re
import struct
import sys
import zlib
from pathlib import Path

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PNG_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
SKIP_DIRS = {".git", "Library", "Temp", "obj", "bin", "Build", "Builds"}


class UnsupportedPng(ValueError):
    pass


def lua_quote(value: str) -> str:
    escaped = ['"']
    for character in value:
        codepoint = ord(character)
        if character == "\\":
            escaped.append("\\\\")
        elif character == '"':
            escaped.append('\\"')
        elif character == "\n":
            escaped.append("\\n")
        elif character == "\r":
            escaped.append("\\r")
        elif character == "\t":
            escaped.append("\\t")
        elif codepoint < 32 or codepoint == 127:
            escaped.append(f"\\{codepoint:03d}")
        else:
            escaped.append(character)
    escaped.append('"')
    return "".join(escaped)


def lua_bytes(payload: bytes) -> str:
    return '"' + "".join(f"\\{value:03d}" for value in payload) + '"'


def png_header(path: Path) -> tuple[int, int] | None:
    try:
        with path.open("rb") as stream:
            header = stream.read(24)
    except OSError:
        return None
    if len(header) < 24 or header[:8] != PNG_SIGNATURE or header[12:16] != b"IHDR":
        return None
    return struct.unpack(">II", header[16:24])


def paeth(left: int, above: int, upper_left: int) -> int:
    estimate = left + above - upper_left
    left_distance = abs(estimate - left)
    above_distance = abs(estimate - above)
    corner_distance = abs(estimate - upper_left)
    if left_distance <= above_distance and left_distance <= corner_distance:
        return left
    if above_distance <= corner_distance:
        return above
    return upper_left


def decode_png(path: Path, max_pixels: int) -> tuple[int, int, bytearray]:
    try:
        source = path.read_bytes()
    except OSError as error:
        raise UnsupportedPng(str(error)) from error
    if not source.startswith(PNG_SIGNATURE):
        raise UnsupportedPng("invalid PNG signature")

    position = len(PNG_SIGNATURE)
    image_header = None
    compressed = bytearray()
    palette = b""
    transparency = b""
    while position + 12 <= len(source):
        chunk_length = struct.unpack_from(">I", source, position)[0]
        chunk_type = source[position + 4:position + 8]
        start = position + 8
        end = start + chunk_length
        if end + 4 > len(source):
            raise UnsupportedPng("truncated PNG chunk")
        chunk = source[start:end]
        if chunk_type == b"IHDR":
            image_header = struct.unpack(">IIBBBBB", chunk)
        elif chunk_type == b"IDAT":
            compressed.extend(chunk)
        elif chunk_type == b"PLTE":
            palette = chunk
        elif chunk_type == b"tRNS":
            transparency = chunk
        elif chunk_type == b"IEND":
            break
        position = end + 4

    if image_header is None:
        raise UnsupportedPng("missing IHDR")
    width, height, bit_depth, color_type, compression, filtering, interlace = image_header
    if width < 1 or height < 1 or width * height > max_pixels:
        raise UnsupportedPng("image exceeds the configured source pixel limit")
    if bit_depth != 8 or color_type not in PNG_CHANNELS or compression != 0 or filtering != 0 or interlace != 0:
        raise UnsupportedPng("only non-interlaced 8-bit PNG images are supported")

    channels = PNG_CHANNELS[color_type]
    stride = width * channels
    try:
        raw = zlib.decompress(compressed)
    except zlib.error as error:
        raise UnsupportedPng(f"PNG decompression failed: {error}") from error
    if len(raw) != height * (stride + 1):
        raise UnsupportedPng("decompressed PNG data has an unexpected length")

    rows: list[bytearray] = []
    previous = bytearray(stride)
    cursor = 0
    for _ in range(height):
        filter_type = raw[cursor]
        cursor += 1
        row = bytearray(raw[cursor:cursor + stride])
        cursor += stride
        if filter_type > 4:
            raise UnsupportedPng("unsupported PNG row filter")
        for index in range(stride):
            left = row[index - channels] if index >= channels else 0
            above = previous[index]
            upper_left = previous[index - channels] if index >= channels else 0
            if filter_type == 1:
                predictor = left
            elif filter_type == 2:
                predictor = above
            elif filter_type == 3:
                predictor = (left + above) // 2
            elif filter_type == 4:
                predictor = paeth(left, above, upper_left)
            else:
                predictor = 0
            row[index] = (row[index] + predictor) & 255
        rows.append(row)
        previous = row

    rgba = bytearray(width * height * 4)
    output = 0
    transparent_gray = struct.unpack(">H", transparency[:2])[0] if color_type == 0 and len(transparency) >= 2 else None
    transparent_rgb = struct.unpack(">HHH", transparency[:6]) if color_type == 2 and len(transparency) >= 6 else None
    for row in rows:
        if color_type == 6:
            rgba[output:output + stride] = row
            output += stride
        elif color_type == 2:
            for index in range(0, stride, 3):
                red, green, blue = row[index:index + 3]
                alpha = 0 if transparent_rgb == (red, green, blue) else 255
                rgba[output:output + 4] = bytes((red, green, blue, alpha))
                output += 4
        elif color_type == 0:
            for gray in row:
                alpha = 0 if transparent_gray == gray else 255
                rgba[output:output + 4] = bytes((gray, gray, gray, alpha))
                output += 4
        elif color_type == 4:
            for index in range(0, stride, 2):
                gray, alpha = row[index:index + 2]
                rgba[output:output + 4] = bytes((gray, gray, gray, alpha))
                output += 4
        else:
            if not palette or len(palette) % 3 != 0:
                raise UnsupportedPng("indexed PNG has no valid palette")
            palette_index_count = len(palette) // 3
            for palette_index in row:
                if palette_index >= palette_index_count:
                    raise UnsupportedPng("indexed PNG refers outside its palette")
                palette_offset = palette_index * 3
                alpha = transparency[palette_index] if palette_index < len(transparency) else 255
                rgba[output:output + 4] = bytes((palette[palette_offset], palette[palette_offset + 1], palette[palette_offset + 2], alpha))
                output += 4
    return width, height, rgba


def downsample_nearest(width: int, height: int, rgba: bytearray, maximum_dimension: int) -> tuple[int, int, bytearray]:
    if maximum_dimension <= 0 or max(width, height) <= maximum_dimension:
        return width, height, rgba
    scale = maximum_dimension / max(width, height)
    output_width = max(1, int(width * scale))
    output_height = max(1, int(height * scale))
    output = bytearray(output_width * output_height * 4)
    for y in range(output_height):
        source_y = min(height - 1, int((y + 0.5) * height / output_height))
        for x in range(output_width):
            source_x = min(width - 1, int((x + 0.5) * width / output_width))
            source_offset = (source_y * width + source_x) * 4
            target_offset = (y * output_width + x) * 4
            output[target_offset:target_offset + 4] = rgba[source_offset:source_offset + 4]
    return output_width, output_height, output


def encode_runs(rgba: bytearray) -> bytes:
    output = bytearray()
    pixel_count = len(rgba) // 4
    pixel_index = 0
    while pixel_index < pixel_count:
        offset = pixel_index * 4
        color = rgba[offset:offset + 4]
        run_length = 1
        while pixel_index + run_length < pixel_count and run_length < 65535:
            next_offset = (pixel_index + run_length) * 4
            if rgba[next_offset:next_offset + 4] != color:
                break
            run_length += 1
        output.extend(struct.pack("<H", run_length))
        output.extend(color)
        pixel_index += run_length
    return bytes(output)


def classify(path: Path) -> str:
    extension = path.suffix.lower()
    if extension == ".png" or extension in {".jpg", ".jpeg", ".tga", ".psd", ".tif", ".tiff"}:
        return "image"
    if extension in {".wav", ".ogg", ".mp3", ".aiff", ".aif"}:
        return "audio"
    if extension == ".prefab":
        return "prefab"
    if extension == ".unity":
        return "scene"
    if extension in {".cs", ".dll"}:
        return "script"
    if extension in {".mat", ".asset", ".shader", ".shadervariants"}:
        return "render-data"
    return "other"


def read_importer_value(meta_text: str, key: str) -> str | None:
    match = re.search(rf"(?m)^\s*{re.escape(key)}:\s*([^\s#]+)", meta_text)
    return match.group(1) if match else None


def load_entries(asset_root: Path, max_source_pixels: int, max_image_dimension: int):
    entries = []
    sprite_payloads = {}
    seen_guids = {}
    for meta_path in sorted(asset_root.rglob("*.meta")):
        if any(part in SKIP_DIRS for part in meta_path.parts):
            continue
        try:
            meta_text = meta_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        guid_match = re.search(r"(?m)^\s*guid:\s*([0-9a-fA-F]{32})\s*$", meta_text)
        if not guid_match:
            continue
        asset_path = Path(str(meta_path)[:-5])
        if not asset_path.is_file():
            continue
        guid = guid_match.group(1).lower()
        if guid in seen_guids:
            raise ValueError(f"duplicate asset GUID {guid}: {seen_guids[guid]} and {asset_path}")
        seen_guids[guid] = asset_path
        relative_path = asset_path.relative_to(asset_root).as_posix()
        entry = {
            "guid": guid,
            "path": relative_path,
            "kind": classify(asset_path),
            "extension": asset_path.suffix.lower(),
            "fileSize": asset_path.stat().st_size,
        }
        for source_key, output_key in (("textureType", "textureType"), ("spriteMode", "spriteMode"), ("spritePixelsToUnits", "spritePixelsPerUnit")):
            value = read_importer_value(meta_text, source_key)
            if value is not None:
                entry[output_key] = value

        if asset_path.suffix.lower() == ".png":
            dimensions = png_header(asset_path)
            if dimensions:
                entry["width"], entry["height"] = dimensions
                try:
                    width, height, rgba = decode_png(asset_path, max_source_pixels)
                    width, height, rgba = downsample_nearest(width, height, rgba, max_image_dimension)
                    rle = encode_runs(rgba)
                    if len(rle) < len(rgba):
                        encoding, payload = "rle", rle
                    else:
                        encoding, payload = "rgba", bytes(rgba)
                    entry["pixelModule"] = guid
                    entry["pixelWidth"] = width
                    entry["pixelHeight"] = height
                    sprite_payloads[guid] = (width, height, encoding, payload)
                except UnsupportedPng as error:
                    entry["pixelError"] = str(error)
        entries.append(entry)
    return entries, sprite_payloads


def emit_asset_module(entries: list[dict]) -> str:
    lines = ["local assets = {"]
    for entry in entries:
        lines.append(f"\t[{lua_quote(entry['guid'])}] = {{")
        for key in ("path", "kind", "extension"):
            lines.append(f"\t\t{key} = {lua_quote(entry[key])},")
        for key in ("fileSize", "width", "height", "pixelWidth", "pixelHeight"):
            if key in entry:
                lines.append(f"\t\t{key} = {entry[key]},")
        for key in ("textureType", "spriteMode", "spritePixelsPerUnit", "pixelError"):
            if key in entry:
                lines.append(f"\t\t{key} = {lua_quote(entry[key])},")
        if "pixelModule" in entry:
            lines.append(f"\t\tpixelModule = {lua_quote(entry['pixelModule'])},")
        lines.append("\t},")
    lines.extend(("}", "return table.freeze(assets)", ""))
    return "\n".join(lines)


def emit_sprite_module(width: int, height: int, encoding: str, payload: bytes) -> str:
    return "\n".join((
        "return table.freeze({",
        f"\twidth = {width},",
        f"\theight = {height},",
        f"\tencoding = {lua_quote(encoding)},",
        f"\tdata = {lua_bytes(payload)},",
        "})",
        "",
    ))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assets", type=Path, default=Path("ppg/People Playground/Assets"), help="Unity Assets directory")
    parser.add_argument("--output", type=Path, default=Path("src/shared/Generated"), help="Generated Luau module directory")
    parser.add_argument("--max-image-dimension", type=int, default=96, help="Nearest-neighbor raster size cap; use 0 to preserve source dimensions")
    parser.add_argument("--max-source-pixels", type=int, default=16_777_216, help="Skip decoding PNGs above this source pixel count")
    arguments = parser.parse_args()

    asset_root = arguments.assets.resolve()
    if not asset_root.is_dir():
        parser.error(f"Assets directory does not exist: {asset_root}")
    if arguments.max_image_dimension < 0 or arguments.max_source_pixels < 1:
        parser.error("image dimension and source pixel limits must be non-negative/positive")

    try:
        entries, sprites = load_entries(asset_root, arguments.max_source_pixels, arguments.max_image_dimension)
    except (OSError, ValueError) as error:
        print(f"asset index generation failed: {error}", file=sys.stderr)
        return 1

    output_root = arguments.output
    sprite_root = output_root / "Sprites"
    output_root.mkdir(parents=True, exist_ok=True)
    sprite_root.mkdir(parents=True, exist_ok=True)
    for stale in output_root.glob("AssetIndex_[0-9a-f][0-9a-f].lua"):
        stale.unlink()
    for stale in sprite_root.glob("*.lua"):
        stale.unlink()

    buckets = {}
    for entry in entries:
        buckets.setdefault(entry["guid"][:2], []).append(entry)
    for prefix, bucket in buckets.items():
        (output_root / f"AssetIndex_{prefix}.lua").write_text(emit_asset_module(bucket), encoding="utf-8")
    for guid, (width, height, encoding, payload) in sprites.items():
        (sprite_root / f"{guid}.lua").write_text(emit_sprite_module(width, height, encoding, payload), encoding="utf-8")

    unsupported = sum(1 for entry in entries if entry.get("kind") == "image" and "pixelError" in entry)
    print(f"Indexed {len(entries)} Unity assets; generated {len(sprites)} PNG pixel modules; {unsupported} images need another decoder.")
    print(f"Generated Luau modules under {output_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
