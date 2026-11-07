"""Bedrock archive handling. No third-party Python dependencies or client required."""
from __future__ import annotations

import json
import re
import shutil
import stat
import tempfile
import uuid
import zipfile
from pathlib import Path


class BedrockError(RuntimeError):
    pass


PACK_KINDS = ("behavior_packs", "resource_packs")
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_EXPANDED_BYTES = 1024 * 1024 * 1024
MAX_MEMBERS = 20000
SNAPSHOT_PACKS = ".blockops-bedrock-packs"


def read_json(path: Path, default=None):
    if not path.exists() and default is not None:
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (ValueError, OSError) as error:
        raise BedrockError(f"Cannot read {path.name}: {error}") from error


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def safe_path(root: Path, relative: str) -> Path:
    """Reject cross-platform traversal, aliases, links and Windows special filenames."""
    parts = relative.rstrip("/").split("/")
    if not parts or any(not p or p in {".", ".."} or p[-1:] in {".", " "} or
                        any(ord(c) < 32 or c in '\\:<>"|?*' for c in p) or
                        re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(\..*)?", p)
                        for p in parts):
        raise BedrockError("The add-on contains an unsafe archive path.")
    path = root.joinpath(*parts)
    if not path.resolve().is_relative_to(root.resolve()) or any(p.is_symlink() for p in [path, *path.parents] if p == root or p.is_relative_to(root)):
        raise BedrockError("The add-on path contains a link or escapes the instance.")
    return path


def version(value):
    if not isinstance(value, list) or len(value) != 3 or any(type(n) is not int or n < 0 for n in value):
        raise BedrockError("Pack versions must be arrays of three nonnegative integers.")
    return value


def pack_manifest(path: Path) -> dict:
    manifest = read_json(path)
    try:
        if type(manifest.get("format_version")) is not int or manifest["format_version"] not in {1, 2}:
            raise BedrockError("Only stable Bedrock manifest format 1 or 2 is supported.")
        header = manifest["header"]
        identifier = str(uuid.UUID(header["uuid"]))
        name = header["name"]
        if not isinstance(name, str) or not name.strip() or len(name) > 250:
            raise BedrockError("Pack names must contain 1–250 characters.")
        modules = manifest["modules"]
        if not isinstance(modules, list) or not modules:
            raise BedrockError("The pack has no modules.")
        types = {m["type"] for m in modules}
        if types <= {"resources"}:
            kind = "resource_packs"
        elif types and types <= {"data", "script"}:
            kind = "behavior_packs"
        else:
            raise BedrockError("Only Bedrock behavior (data/script) and resource packs are supported.")
        module_ids = set()
        for module in modules:
            module_id = str(uuid.UUID(module["uuid"]))
            if module_id == identifier or module_id in module_ids:
                raise BedrockError("Header and module UUIDs must be distinct.")
            module_ids.add(module_id)
            version(module["version"])
        dependencies = manifest.get("dependencies", [])
        if not isinstance(dependencies, list):
            raise BedrockError("Pack dependencies must be a list.")
        for dep in dependencies:
            if "uuid" in dep:
                uuid.UUID(dep["uuid"])
                version(dep["version"])
            elif not isinstance(dep.get("module_name"), str) or not isinstance(dep.get("version"), str):
                raise BedrockError("Invalid pack dependency.")
        minimum = header.get("min_engine_version")
        if minimum is not None:
            version(minimum)
        return {"id": identifier, "name": name, "kind": kind,
                "version": version(header["version"]), "minimumEngineVersion": minimum,
                "dependencies": dependencies, "scripted": "script" in types}
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        raise BedrockError(f"Invalid Bedrock manifest in {path.parent.name}.") from error


def unpack_archive(source, destination: Path, budget: dict, depth=0):
    if depth > 2:
        raise BedrockError("The add-on contains too many nested archives.")
    try:
        with zipfile.ZipFile(source) as archive:
            seen = set()
            for member in archive.infolist():
                budget["members"] += 1
                budget["bytes"] += member.file_size
                if budget["members"] > MAX_MEMBERS or budget["bytes"] > MAX_EXPANDED_BYTES:
                    raise BedrockError("The add-on exceeds the extraction safety limit.")
                key = member.filename.rstrip("/").casefold()
                if key in seen or member.flag_bits & 1:
                    raise BedrockError("Duplicate paths or encrypted add-ons are not supported.")
                seen.add(key)
                mode = member.external_attr >> 16
                if stat.S_IFMT(mode) not in {0, stat.S_IFREG, stat.S_IFDIR}:
                    raise BedrockError("The add-on contains a link or special file.")
                path = safe_path(destination, member.filename)
                if member.is_dir():
                    path.mkdir(parents=True, exist_ok=True)
                    continue
                path.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as src, path.open("xb") as out:
                    shutil.copyfileobj(src, out, length=1024 * 1024)
            for nested in list(destination.rglob("*")):
                if nested.is_file() and nested.suffix.lower() in {".mcpack", ".mcaddon"}:
                    if nested.stat().st_size > MAX_ARCHIVE_BYTES:
                        raise BedrockError("The nested add-on is too large.")
                    target = nested.with_name(nested.name + ".unpacked")
                    target.mkdir()
                    unpack_archive(nested, target, budget, depth + 1)
    except (zipfile.BadZipFile, RuntimeError, OSError, EOFError) as error:
        if isinstance(error, BedrockError):
            raise
        raise BedrockError(f"Cannot extract the add-on: {error}") from error


def activations(world: Path, kind: str) -> list:
    entries = read_json(safe_path(world, f"world_{kind}.json"), [])
    if not isinstance(entries, list) or any(not isinstance(e, dict) or "pack_id" not in e or "version" not in e for e in entries):
        raise BedrockError(f"Invalid world_{kind}.json. Fix it before managing packs.")
    return entries


def list_packs(folder: Path, world: Path) -> list[dict]:
    packs = []
    for kind in PACK_KINDS:
        enabled = {str(e["pack_id"]).lower(): e["version"] for e in activations(world, kind)}
        root = safe_path(folder, kind)
        if not root.exists():
            continue
        for child in sorted(root.iterdir()):
            # Mojang ships many built-in packs here (including editor-only
            # module types). Only UUID folders installed by this importer are
            # managed; keep all bundled or externally managed packs untouched.
            try:
                uuid.UUID(child.name)
            except ValueError:
                continue
            if not child.is_dir() or child.is_symlink() or not (child / "manifest.json").is_file():
                continue
            pack = pack_manifest(safe_path(folder, f"{kind}/{child.name}/manifest.json"))
            if pack["kind"] != kind:
                raise BedrockError(f"Pack {child.name} is in the wrong folder.")
            pack["directory"] = child.name
            pack["enabled"] = enabled.get(pack["id"]) == pack["version"]
            packs.append(pack)
    ids = [p["id"] for p in packs]
    if len(ids) != len(set(ids)):
        raise BedrockError("Duplicate installed pack UUIDs. Resolve them before managing packs.")
    return packs


def check_dependencies(packs: list[dict], enabled: set[str]):
    by_id = {p["id"]: p for p in packs}
    for identifier in enabled:
        pack = by_id[identifier]
        for dependency in pack["dependencies"]:
            if "uuid" not in dependency:
                continue  # Built-in script API dependencies are checked by BDS at startup.
            dep_id = str(uuid.UUID(dependency["uuid"]))
            dep = by_id.get(dep_id)
            if dep_id not in enabled or dep is None or dep["version"] != dependency["version"]:
                raise BedrockError(f"{pack['name']} requires pack {dep_id} version {dependency['version']}. Install and activate it first.")


def replace_transaction(folder: Path, replacements: list[tuple[Path, Path | None]]):
    """Commit prepared paths, restoring all original paths if any replacement fails."""
    with tempfile.TemporaryDirectory(prefix=".blockops-pack-rollback-", dir=folder) as temporary:
        previous = []
        try:
            for index, (target, source) in enumerate(replacements):
                safe_path(folder, target.relative_to(folder).as_posix())
                backup = Path(temporary) / str(index)
                existed = target.exists()
                if existed:
                    target.replace(backup)
                previous.append((target, backup, existed))
                if source is not None:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    source.replace(target)
        except Exception:
            for target, backup, existed in reversed(previous):
                if target.is_dir():
                    shutil.rmtree(target)
                elif target.exists():
                    target.unlink()
                if existed:
                    backup.replace(target)
            raise


def import_packs(folder: Path, world: Path, archive: Path) -> list[dict]:
    """Validate the entire bundle, then install inactive packs atomically, without upgrades."""
    existing = list_packs(folder, world)
    known = {p["id"] for p in existing}
    with tempfile.TemporaryDirectory(prefix=".blockops-addon-", dir=folder) as temporary:
        staging = Path(temporary)
        unpack_archive(archive, staging, {"members": 0, "bytes": 0})
        manifests = sorted(staging.rglob("manifest.json"))
        if not manifests:
            raise BedrockError("No manifest.json found. Choose an unpacked-compatible Bedrock .mcpack or .mcaddon.")
        if len(manifests) > 100:
            raise BedrockError("An add-on may contain at most 100 packs.")
        packs, replacements, roots = [], [], []
        for manifest in manifests:
            root = manifest.parent
            if any(root.is_relative_to(other) or other.is_relative_to(root) for other in roots):
                raise BedrockError("Nested pack manifests are not supported.")
            roots.append(root)
            pack = pack_manifest(manifest)
            if pack["id"] in known:
                raise BedrockError(f"Pack {pack['name']} is already installed or has a duplicate UUID. Remove its old version while stopped before importing an update.")
            known.add(pack["id"])
            target = safe_path(folder, f"{pack['kind']}/{pack['id']}")
            if target.exists():
                raise BedrockError("A pack destination already exists; nothing was overwritten.")
            replacements.append((target, root))
            packs.append(pack)
        replace_transaction(folder, replacements)
        return packs


def change_pack(folder: Path, world: Path, identifier: str, action: str):
    if action not in {"enable", "disable", "remove"}:
        raise BedrockError("Choose enable, disable, or remove.")
    packs = list_packs(folder, world)
    pack = next((p for p in packs if p["id"] == identifier), None)
    if pack is None:
        raise BedrockError("That pack is not installed in this instance.")
    enabled = {p["id"] for p in packs if p["enabled"]}
    # Enabling includes installed UUID dependencies, including paired resource packs.
    def include(pack_id, visiting):
        if pack_id in visiting:
            return
        visiting.add(pack_id)
        candidate = next((p for p in packs if p["id"] == pack_id), None)
        if candidate is None:
            return
        enabled.add(pack_id)
        for dependency in candidate["dependencies"]:
            if "uuid" in dependency:
                include(str(uuid.UUID(dependency["uuid"])), visiting)
    if action == "enable":
        include(identifier, set())
    else:
        enabled.discard(identifier)
    check_dependencies(packs, enabled)
    with tempfile.TemporaryDirectory(prefix=".blockops-activation-", dir=folder) as temporary:
        replacements = []
        for kind in PACK_KINDS:
            entries = activations(world, kind)
            # Preserve order and unknown externally managed references.
            entries = [e for e in entries if str(e["pack_id"]).lower() not in {p["id"] for p in packs}]
            entries.extend({"pack_id": p["id"], "version": p["version"]} for p in packs if p["kind"] == kind and p["id"] in enabled)
            source = Path(temporary) / f"world_{kind}.json"
            write_json(source, entries)
            replacements.append((safe_path(world, source.name), source))
        if action == "remove":
            replacements.append((safe_path(folder, f"{pack['kind']}/{pack['directory']}"), None))
        replace_transaction(folder, replacements)
