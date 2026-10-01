def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _canonical_json_digest(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return _sha256(raw)


def _reviewed_text_sha256(raw: bytes, suffix: str) -> tuple[str, str]:
    raw_digest = _sha256(raw)
    if suffix.casefold() in {".py", ".gd"}:
        text = raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
        return _sha256(text.encode("utf-8")), raw_digest
    return raw_digest, raw_digest


def _strict_int(value: Any, field: str) -> int:
    if type(value) is bool or not isinstance(value, int):
        raise ValueError(f"{field} must be a strict integer")
    return value


def _strict_float(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{field} must be finite")
    return result


def _strict_nonempty_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _strict_sha256_field(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError(f"{field} must be a lowercase sha256 hex digest")
    return value


_MANIFEST_FILE_ENTRY_KEYS = frozenset({"path", "role", "size", "sha256"})


def _validate_json_value(value: Any, *, path: str, depth: int) -> None:
    if depth > _MAX_JSON_DEPTH:
        raise ValueError(f"JSON depth limit exceeded at {path}")
    if isinstance(value, dict):
        if len(value) > _MAX_JSON_OBJECT_KEYS:
            raise ValueError(f"JSON object too large at {path}")
        for key, child in value.items():
            if not isinstance(key, str):
                raise ValueError(f"JSON object key must be string at {path}")
            _validate_json_value(child, path=f"{path}.{key}", depth=depth + 1)
        return
    if isinstance(value, list):
        if len(value) > _MAX_JSON_ARRAY_LEN:
            raise ValueError(f"JSON array too large at {path}")
        for index, child in enumerate(value):
            _validate_json_value(child, path=f"{path}[{index}]", depth=depth + 1)
        return
    if value is None or isinstance(value, (bool, str)):
        return
    if type(value) is bool:
        return
    if isinstance(value, int):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"non-finite JSON number at {path}")
        return
    raise ValueError(f"unsupported JSON value at {path}")


def _strict_region_box(box: Any, field: str) -> dict[str, list[float]]:
    if not isinstance(box, dict):
        raise ValueError(f"{field} must be an object")
    mn = box.get("min")
    mx = box.get("max")
    if not isinstance(mn, list) or not isinstance(mx, list) or len(mn) != 3 or len(mx) != 3:
        raise ValueError(f"{field} min/max invalid")
    return {
        "min": [_strict_float(mn[i], f"{field}.min[{i}]") for i in range(3)],
        "max": [_strict_float(mx[i], f"{field}.max[{i}]") for i in range(3)],
    }


def _bundle_path_crosses_link(path: Path) -> bool:
    for current in [path, *path.parents]:
        if _linked(current):
            return True
        if current.parent == current:
            break
    return False


def _path(value: Any) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise ValueError(f"unsafe bundle path: {value!r}")
    pure = PurePosixPath(value)
    if (
        pure.is_absolute()
        or pure.as_posix() != value
        or any(part in {"", ".", ".."} or ":" in part for part in value.split("/"))
    ):
        raise ValueError(f"unsafe bundle path: {value!r}")
    return value


def _linked(path: Path) -> bool:
    junction = getattr(path, "is_junction", None)
    return path.is_symlink() or bool(junction and junction())


def _resolve_under_root(root: Path, rel: str) -> Path:
    current = root
    for part in PurePosixPath(rel).parts:
        current = current / part
        if _linked(current):
            raise ValueError(f"symlink or junction in bundle path: {rel}")
    return current


def _read_bounded_bytes(path: Path, declared_size: int) -> bytes:
    if _linked(path):
        raise ValueError(f"symlink or junction file: {path.name}")
    if type(declared_size) is not int or declared_size < 0:
        raise ValueError(f"invalid declared size for {path.name}")
    try:
        stat = path.stat()
    except OSError as exc:
        raise ValueError(f"cannot stat bundle file: {path.name}") from exc
    if not path.is_file():
        raise ValueError(f"bundle path is not a regular file: {path.name}")
    actual = stat.st_size
    if actual > _MAX_FILE_BYTES:
        raise ValueError(f"bundle file exceeds size limit: {path.name}")
    if actual != declared_size:
        raise ValueError(f"declared size {declared_size} != actual {actual} for {path.name}")
    with path.open("rb") as handle:
        return handle.read(actual)


def _object_bytes(raw: bytes, name: str) -> dict[str, Any]:
    if len(raw) > _MAX_JSON_BYTES:
        raise ValueError(f"JSON file exceeds size limit: {name}")

    def unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    value = json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=unique_pairs,
        parse_constant=lambda token: (_ for _ in ()).throw(ValueError(token)),
    )
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {name}")
    _validate_json_value(value, path=name, depth=0)
    return value


def _object(path: Path, declared_size: int) -> dict[str, Any]:
    return _object_bytes(_read_bounded_bytes(path, declared_size), path.name)


def _parse_contract(data: dict[str, Any]) -> dict[str, Any]:
    bones = data.get("bones")
    if not isinstance(bones, list):
        raise ValueError("contract bones missing")
    bases_raw = data.get("rest_joint_bases")
    if not isinstance(bases_raw, dict):
        raise ValueError("contract rest_joint_bases missing")
    rest_bases = {
        k: {"y_axis": v["y_axis"], "neg_z_axis": v["neg_z_axis"]}
        for k, v in bases_raw.items()
        if isinstance(v, dict)
    }
    return {
        **data,
        "_bones": bones,
        "_rest_bases": rest_bases,
        "rest_joint_origins": data.get("rest_joint_origins", {}),
    }


def _finding_dicts(findings: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in findings:
        if not isinstance(item, dict):
            raise ValueError("validation finding must be an object")
        out.append(item)
    return out


def _finding_key(finding: dict[str, Any]) -> tuple[str, str, str, str, str, str]:
    return (
        str(finding.get("rule_id", "")),
        str(finding.get("severity", "")),
        str(finding.get("expected", "")),
        str(finding.get("actual", "")),
        str(finding.get("artifact", "")),
        str(finding.get("message", "")),
    )


def _recomputed_validation_status(findings: list[dict[str, Any]]) -> str:
    if any(f.get("severity") == "FAIL" for f in findings):
        return "FAIL"
    return "PASS"


_REGION_BOUNDARY_TOLERANCE = 1e-6
_RUNTIME_REQUEST_STRICT_INT_FIELDS = frozenset(
    {
        "vertex_count",
        "affected_vertex_count",
        "unaffected_vertex_count",
    }
)


def _strict_runtime_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a strict integer")
    as_float = float(value)
    if not math.isfinite(as_float) or as_float != math.trunc(as_float):
        raise ValueError(f"{field} must be a strict integer")
    iv = int(as_float)
    if iv < 0:
        raise ValueError(f"{field} must be non-negative")
    return iv


def _strict_region_boundary_tolerance(value: Any) -> float:
    tol = _strict_float(value, "region_boundary_tolerance")
    if not math.isclose(tol, _REGION_BOUNDARY_TOLERANCE, rel_tol=0.0, abs_tol=0.0):
        raise ValueError("region_boundary_tolerance must match pinned REGION_BOUNDARY_TOLERANCE")
    return float(f"{_REGION_BOUNDARY_TOLERANCE:.6f}")


def _canonicalize_runtime_request_for_digest(request: dict[str, Any]) -> dict[str, Any]:
    payload = {
        k: v for k, v in request.items() if k not in {"output_path", "request_digest", "glb"}
    }
    out: dict[str, Any] = {}
    for key in sorted(payload):
        value = payload[key]
        if key in _RUNTIME_REQUEST_STRICT_INT_FIELDS:
            out[key] = _strict_runtime_int(value, key)
        elif key == "region_boundary_tolerance":
            out[key] = _strict_region_boundary_tolerance(value)
        else:
            out[key] = value
    return out


def _godot_compatible_runtime_json_bytes(payload: dict[str, Any]) -> bytes:
    parts: list[str] = []
    for key in sorted(payload):
        value = payload[key]
        if key == "region_boundary_tolerance":
            value_json = "0.000001"
        else:
            value_json = json.dumps(
                value, sort_keys=True, separators=(",", ":"), allow_nan=False
            )
        parts.append(f"{json.dumps(key)}:{value_json}")
    return ("{" + ",".join(parts) + "}").encode("utf-8")


def _runtime_request_digest(request: dict[str, Any]) -> str:
    payload = _canonicalize_runtime_request_for_digest(request)
    return _sha256(_godot_compatible_runtime_json_bytes(payload))


def _axis_angle_basis(axis: list[Any], degrees: float) -> list[list[float]]:
    ax = _strict_float(axis[0], "rotation_axis[0]")
    ay = _strict_float(axis[1], "rotation_axis[1]")
    az = _strict_float(axis[2], "rotation_axis[2]")
    length = math.sqrt(ax * ax + ay * ay + az * az)
    if length < 1e-12:
        raise ValueError("rotation_axis is zero")
    ax, ay, az = ax / length, ay / length, az / length
    rad = math.radians(degrees)
    c, s = math.cos(rad), math.sin(rad)
    t = 1.0 - c
    return [
        [t * ax * ax + c, t * ax * ay - s * az, t * ax * az + s * ay],
        [t * ax * ay + s * az, t * ay * ay + c, t * ay * az - s * ax],
        [t * ax * az - s * ay, t * ay * az + s * ax, t * az * az + c],
    ]


def _basis_close(expected: list[list[float]], observed: list[list[float]], tol: float) -> bool:
    for row in range(3):
        for col in range(3):
            if abs(expected[row][col] - observed[row][col]) > tol:
                return False
    return True


def _inside(
    p: tuple[float, float, float],
    box: dict[str, Any],
    *,
    boundary_tolerance: float = _REGION_BOUNDARY_TOLERANCE,
) -> bool:
    mn, mx = box["min"], box["max"]
    eps = boundary_tolerance
    return (
        (p[0] - float(mx[0])) <= eps
        and (float(mn[0]) - p[0]) <= eps
        and (p[1] - float(mx[1])) <= eps
        and (float(mn[1]) - p[1]) <= eps
        and (p[2] - float(mx[2])) <= eps
        and (float(mn[2]) - p[2]) <= eps
    )


def _match_rest_to_glb(
    samples: list[dict[str, Any]],
    positions: list[Any] | tuple[Any, ...],
    *,
    rest_tol: float,
) -> dict[int, int]:
    vertex_count = len(positions)
    if vertex_count > _MAX_RUNTIME_VERTICES:
        raise ValueError("vertex count exceeds cold runtime matching limit")
    godot_indices: set[int] = set()
    for entry in samples:
        idx = _strict_int(entry.get("vertex_index"), "vertex_index")
        if idx in godot_indices or idx < 0 or idx >= vertex_count:
            raise ValueError("vertex_index coverage or uniqueness violation")
        godot_indices.add(idx)
    if godot_indices != set(range(vertex_count)):
        raise ValueError("vertex_samples do not cover all vertex indices")
    glb_pool = list(range(vertex_count))
    mapping: dict[int, int] = {}
    for entry in sorted(
        samples, key=lambda item: _strict_int(item.get("vertex_index"), "vertex_index")
    ):
        godot_idx = _strict_int(entry.get("vertex_index"), "vertex_index")
        rest = entry.get("rest_position")
        if not isinstance(rest, list) or len(rest) != 3:
            raise ValueError("vertex sample rest_position invalid")
        rp = (
            _strict_float(rest[0], "rest_position[0]"),
            _strict_float(rest[1], "rest_position[1]"),
            _strict_float(rest[2], "rest_position[2]"),
        )
        best_glb = -1
        best_dist = float("inf")
        for glb_idx in glb_pool:
            glb_rest = positions[glb_idx]
            dist = math.sqrt(sum((rp[axis] - float(glb_rest[axis])) ** 2 for axis in range(3)))
            if dist < best_dist:
                best_dist = dist
                best_glb = glb_idx
        if best_glb < 0 or best_dist > rest_tol:
            raise ValueError("rest_position does not match any unused GLB vertex")
        glb_pool.remove(best_glb)
        mapping[godot_idx] = best_glb
    if glb_pool:
        raise ValueError("vertex_samples do not account for full GLB geometry")
    return mapping


def _recompute_runtime(
    payload: dict[str, Any],
    decoded: DecodedInternalSkinnedGLB,
    contract: dict[str, Any],
    *,
    boundary_tolerance: float = _REGION_BOUNDARY_TOLERANCE,
) -> tuple[float, float, int, int]:
    oracle = contract["deformation_oracle"]
    affected = oracle["affected_region"]
    unaffected = oracle["unaffected_region"]
    samples = payload.get("vertex_samples")
    if not isinstance(samples, list):
        raise ValueError("vertex_samples missing")
    positions = decoded.primitive.positions
    vertex_count = _strict_int(payload.get("vertex_count"), "vertex_count")
    if vertex_count != len(positions):
        raise ValueError("vertex_count does not match decoded GLB")
    if len(samples) != vertex_count:
        raise ValueError("vertex_samples must include every vertex")
    _match_rest_to_glb(samples, positions, rest_tol=1e-3)
    max_aff = 0.0
    max_unaff = 0.0
    aff = 0
    unaff = 0
    for entry in samples:
        if not isinstance(entry, dict):
            raise ValueError("vertex sample must be an object")
        rest = entry.get("rest_position")
        posed = entry.get("posed_position")
        if (
            not isinstance(rest, list)
            or not isinstance(posed, list)
            or len(rest) != 3
            or len(posed) != 3
        ):
            raise ValueError("vertex sample positions must be length-3 arrays")
        rp = (
            _strict_float(rest[0], "rest_position[0]"),
            _strict_float(rest[1], "rest_position[1]"),
            _strict_float(rest[2], "rest_position[2]"),
        )
        pp = (
            _strict_float(posed[0], "posed_position[0]"),
            _strict_float(posed[1], "posed_position[1]"),
            _strict_float(posed[2], "posed_position[2]"),
        )
        delta = math.sqrt(sum((pp[i] - rp[i]) ** 2 for i in range(3)))
        if _inside(rp, affected, boundary_tolerance=boundary_tolerance):
            aff += 1
            max_aff = max(max_aff, delta)
        if _inside(rp, unaffected, boundary_tolerance=boundary_tolerance):
            unaff += 1
            max_unaff = max(max_unaff, delta)
    return max_aff, max_unaff, aff, unaff


def _population_from_glb(
    decoded: DecodedInternalSkinnedGLB,
    contract: dict[str, Any],
    *,
    boundary_tolerance: float = _REGION_BOUNDARY_TOLERANCE,
) -> tuple[int, int]:
    oracle = contract["deformation_oracle"]
    affected = oracle["affected_region"]
    unaffected = oracle["unaffected_region"]
    aff = 0
    unaff = 0
    for pos in decoded.primitive.positions:
        rp = (float(pos[0]), float(pos[1]), float(pos[2]))
        if _inside(rp, affected, boundary_tolerance=boundary_tolerance):
            aff += 1
        if _inside(rp, unaffected, boundary_tolerance=boundary_tolerance):
            unaff += 1
    return aff, unaff


def _validate_source_declaration(
    source_decl: dict[str, Any],
    *,
    contract_bytes_hash: str,
    contract_canonical_hash: str,
) -> None:
    if source_decl.get("schema_version") != "internal-rig-source-declaration-0.8.0":
        raise ValueError("source_declaration schema_version mismatch")
    if source_decl.get("validator_contract_version") != VALIDATOR_CONTRACT_VERSION:
        raise ValueError("source_declaration validator_contract_version mismatch")
    if source_decl.get("contract_bytes_sha256") != contract_bytes_hash:
        raise ValueError("source_declaration contract_bytes_sha256 mismatch")
    if source_decl.get("contract_canonical_sha256") != contract_canonical_hash:
        raise ValueError("source_declaration contract_canonical_sha256 mismatch")
    pin = source_decl.get("positive_fixture_glb_sha256")
    if not isinstance(pin, str) or not _SHA256.fullmatch(pin):
        raise ValueError("source_declaration positive_fixture_glb_sha256 invalid")


def _enforce_runtime_request(
    request: dict[str, Any],
    contract: dict[str, Any],
    harness_lf: str,
    *,
    vertex_count: int,
    affected_count: int,
    unaffected_count: int,
) -> None:
    oracle = contract["deformation_oracle"]
    _strict_nonempty_string(request.get("godot_version"), "runtime request godot_version")
    if request.get("pose_bone") != oracle.get("pose_bone"):
        raise ValueError("runtime request pose_bone does not match contract")
    axis = request.get("rotation_axis")
    if not isinstance(axis, list) or len(axis) != 3:
        raise ValueError("runtime request rotation_axis invalid")
    req_axis = [_strict_float(axis[i], f"rotation_axis[{i}]") for i in range(3)]
    oracle_axis = oracle.get("rotation_axis", [])
    if not isinstance(oracle_axis, list) or len(oracle_axis) != 3:
        raise ValueError("contract rotation_axis invalid")
    if req_axis != [_strict_float(oracle_axis[i], "contract.rotation_axis") for i in range(3)]:
        raise ValueError("runtime request rotation_axis does not match contract")
    if _strict_float(request.get("rotation_degrees"), "rotation_degrees") != float(
        oracle.get("rotation_degrees")
    ):
        raise ValueError("runtime request rotation_degrees does not match contract")
    for key in ("affected", "unaffected"):
        contract_key = "affected_region" if key == "affected" else "unaffected_region"
        contract_box = oracle.get(contract_key)
        if not isinstance(contract_box, dict):
            raise ValueError(f"contract {contract_key} missing")
        if _strict_region_box(request.get(key), key) != _strict_region_box(
            contract_box, contract_key
        ):
            raise ValueError(f"runtime request {key} does not match contract")
    if _strict_int(request.get("vertex_count"), "vertex_count") != vertex_count:
        raise ValueError("runtime request vertex_count does not match decoded GLB")
    if _strict_int(request.get("affected_vertex_count"), "affected_vertex_count") != affected_count:
        raise ValueError("runtime request affected_vertex_count does not match decoded GLB")
    if (
        _strict_int(request.get("unaffected_vertex_count"), "unaffected_vertex_count")
        != unaffected_count
    ):
        raise ValueError("runtime request unaffected_vertex_count does not match decoded GLB")
    if _strict_float(
        request.get("min_affected_displacement"), "min_affected_displacement"
    ) != float(oracle.get("min_affected_displacement")):
        raise ValueError("runtime request min_affected_displacement mismatch")
    if _strict_float(
        request.get("max_unaffected_displacement"), "max_unaffected_displacement"
    ) != float(oracle.get("max_unaffected_displacement")):
        raise ValueError("runtime request max_unaffected_displacement mismatch")
    if _strict_float(
        request.get("max_affected_displacement"), "max_affected_displacement"
    ) != float(oracle.get("max_affected_displacement")):
        raise ValueError("runtime request max_affected_displacement mismatch")
    _strict_region_boundary_tolerance(request.get("region_boundary_tolerance"))
    if request.get("harness_sha256") != harness_lf:
        raise ValueError("runtime request harness_sha256 does not match reviewed harness pin")


def _validate_observation_pose(request: dict[str, Any], observation: dict[str, Any]) -> None:
    transform = observation.get("observed_bone_transform")
    if not isinstance(transform, dict) or transform.get("kind") != "basis":
        raise ValueError("observed_bone_transform must be a basis object")
    basis = transform.get("basis")
    if not isinstance(basis, list) or len(basis) != 3:
        raise ValueError("observed_bone_transform basis invalid")
    observed: list[list[float]] = []
    for row_index, row in enumerate(basis):
        if not isinstance(row, list) or len(row) != 3:
            raise ValueError("observed_bone_transform basis row invalid")
        observed.append(
            [_strict_float(row[c], f"basis[{row_index}][{c}]") for c in range(3)]
        )
    expected = _axis_angle_basis(
        list(request.get("rotation_axis", [])),
        _strict_float(request.get("rotation_degrees"), "rotation_degrees"),
    )
    if not _basis_close(expected, observed, _BASIS_TOL):
        raise ValueError("observed bone rotation does not match requested pose")


def verify_bundle(bundle: Path) -> dict[str, Any]:
    bundle = Path(bundle)
    if _bundle_path_crosses_link(bundle):
        raise ValueError("bundle path crosses a symlink or junction")
    if _linked(bundle):
        raise ValueError("bundle root is a symlink or junction")
    root = bundle.resolve(strict=True)
    manifest_path = _resolve_under_root(root, "manifest.json")
    manifest_entry_size = manifest_path.stat().st_size
    if manifest_entry_size > _MAX_JSON_BYTES:
        raise ValueError("manifest.json exceeds size limit")
    manifest = _object(manifest_path, manifest_entry_size)
    if manifest.get("schema_version") != "rig-evidence-0.8.0":
        raise ValueError("unsupported manifest schema")
    _strict_nonempty_string(manifest.get("bundle_id"), "manifest.bundle_id")
    _strict_sha256_field(
        manifest.get("harness_reviewed_sha256"), "manifest.harness_reviewed_sha256"
    )
    _strict_sha256_field(
        manifest.get("blender_script_reviewed_sha256"),
        "manifest.blender_script_reviewed_sha256",
    )
    manifest_runtime_status = manifest.get("runtime_status")
    if manifest_runtime_status is None:
        raise ValueError("manifest runtime_status required")
    if manifest_runtime_status != "PASS":
        raise ValueError("manifest runtime_status must be PASS")
    files = manifest.get("files")
    if not isinstance(files, list):
        raise ValueError("manifest files must be an array")
    roles: dict[str, list[dict[str, Any]]] = {}
    listed: set[str] = set()
    total_size = 0
    file_raw: dict[str, bytes] = {}
    for item in files:
        if not isinstance(item, dict):
            raise ValueError("manifest file entry must be an object")
        unknown_keys = set(item.keys()) - _MANIFEST_FILE_ENTRY_KEYS
        if unknown_keys:
            raise ValueError("manifest file entry has unknown keys")
        rel = _path(item.get("path"))
        role = item.get("role")
        if rel in listed or role not in _SINGLE_ROLES:
            raise ValueError(f"duplicate or unknown bundle entry: {rel}")
        digest, size = item.get("sha256"), item.get("size")
        if not isinstance(digest, str) or not _SHA256.fullmatch(digest) or type(size) is not int:
            raise ValueError(f"invalid hash or size: {rel}")
        if size < 0:
            raise ValueError(f"invalid declared size: {rel}")
        total_size += size
        if size > _MAX_FILE_BYTES or total_size > _MAX_BUNDLE_BYTES:
            raise ValueError("bundle exceeds size limits")
        target = _resolve_under_root(root, rel)
        if not target.is_file():
            raise ValueError(f"missing bundle file: {rel}")
        raw = _read_bounded_bytes(target, size)
        if _sha256(raw) != digest:
            raise ValueError(f"hash mismatch: {rel}")
        listed.add(rel)
        file_raw[rel] = raw
        roles.setdefault(str(role), []).append(item)
    for role in _SINGLE_ROLES:
        if len(roles.get(role, [])) != 1:
            raise ValueError(f"bundle requires exactly one {role}")
    on_disk: set[str] = set()
    for current, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        base = Path(current)
        if _linked(base):
            rel = base.relative_to(root).as_posix() or "."
            raise ValueError(f"symlink or junction in bundle inventory: {rel}")
        for name in list(dirnames) + filenames:
            child = base / name
            if _linked(child):
                rel = child.relative_to(root).as_posix()
                raise ValueError(f"symlink or junction in bundle inventory: {rel}")
        for name in filenames:
            on_disk.add((base / name).relative_to(root).as_posix())
    allowed_extra = _EXCLUDED
    if on_disk - allowed_extra != listed:
        raise ValueError("unlisted or missing manifest files")

    def one(role: str) -> dict[str, Any]:
        return roles[role][0]

    glb_entry = one("skinned_glb")
    glb_path = _resolve_under_root(root, glb_entry["path"])
    contract_entry = one("rig_verification_contract")
    contract_raw = file_raw[contract_entry["path"]]
    contract_data = _parse_contract(_object_bytes(contract_raw, contract_entry["path"]))
    contract_bytes_hash = _sha256(contract_raw)
    contract_canonical_hash = _canonical_json_digest(
        _object_bytes(contract_raw, contract_entry["path"])
    )

    reviewed_blender = file_raw[one("reviewed_blender_export_script")["path"]]
    reviewed_harness = file_raw[one("reviewed_godot_harness")["path"]]
    blender_lf, _ = _reviewed_text_sha256(reviewed_blender, ".py")
    harness_lf, _harness_raw = _reviewed_text_sha256(reviewed_harness, ".gd")

    if contract_canonical_hash != PINNED_CONTRACT_CANONICAL_SHA256:
        raise ValueError("contract canonical digest does not match pinned reviewed contract")
    if blender_lf != PINNED_BLENDER_EXPORT_SCRIPT_SHA256:
        raise ValueError("reviewed blender export script does not match pin")
    if harness_lf != PINNED_GODOT_HARNESS_REVIEWED_SHA256:
        raise ValueError("reviewed godot harness LF digest does not match pin")

    if manifest.get("contract_id") != contract_data.get("contract_id"):
        raise ValueError("manifest contract_id does not match bundled contract")
    if manifest.get("glb_sha256") != glb_entry["sha256"]:
        raise ValueError("manifest glb_sha256 mismatch")
    if manifest.get("contract_sha256") != contract_entry["sha256"]:
        raise ValueError("manifest contract_sha256 mismatch")
    if manifest.get("harness_reviewed_sha256") != harness_lf:
        raise ValueError("manifest harness_reviewed_sha256 mismatch")
    if manifest.get("blender_script_reviewed_sha256") != blender_lf:
        raise ValueError("manifest blender_script_reviewed_sha256 mismatch")

    decoded = _decode_internal_skinned_glb(glb_path)
    if decoded.sha256 != glb_entry["sha256"]:
        raise ValueError("skinned_glb digest mismatch after decode")
    findings = run_internal_skin_checks(decoded, contract_data)
    finding_dicts = _finding_dicts(findings)
    recomputed_status = _recomputed_validation_status(finding_dicts)
    report = _object_bytes(
        file_raw[one("rig_validation_report")["path"]], "rig_validation_report.json"
    )
    if report.get("schema_version") != "rig-validation-report-0.8.0":
        raise ValueError("validation report schema mismatch")
    if report.get("validator_contract_version") != VALIDATOR_CONTRACT_VERSION:
        raise ValueError("validation report validator_contract_version mismatch")
    if report.get("contract_canonical_sha256") != contract_canonical_hash:
        raise ValueError("validation report contract_canonical_sha256 mismatch")
    if report.get("glb_sha256") != glb_entry["sha256"]:
        raise ValueError("validation report glb_sha256 mismatch")
    if report.get("contract_id") != contract_data.get("contract_id"):
        raise ValueError("validation report contract_id mismatch")
    if report.get("contract_sha256") != contract_entry["sha256"]:
        raise ValueError("validation report contract_sha256 mismatch")
    bundled_findings = report.get("findings")
    if not isinstance(bundled_findings, list):
        raise ValueError("validation report findings missing")
    if len(bundled_findings) != len(finding_dicts):
        raise ValueError("validation report findings length mismatch")
    for finding in bundled_findings:
        if not isinstance(finding, dict):
            raise ValueError("validation report finding must be an object")
    bundled_keys = [_finding_key(f) for f in bundled_findings]
    recomputed_keys = [_finding_key(f) for f in finding_dicts]
    if bundled_keys != recomputed_keys:
        raise ValueError("validation findings mismatch with recomputed static checks")
    if report.get("status") != recomputed_status:
        raise ValueError("validation report status does not match recomputed findings")
    if "passed" in report:
        report_passed = report["passed"]
        if type(report_passed) is not bool:
            raise ValueError("validation report passed must be a boolean")
        if report_passed != (recomputed_status == "PASS"):
            raise ValueError("validation report passed does not match recomputed status")
    if "summary" in report:
        if not isinstance(report["summary"], str):
            raise ValueError("validation report summary must be a string")
    if recomputed_status != "PASS":
        raise ValueError("static validation FAIL rejected")
    if manifest.get("validation_status") != recomputed_status:
        raise ValueError("manifest validation_status mismatch")

    request = _object_bytes(
        file_raw[one("rig_runtime_request")["path"]], "rig_runtime_request.json"
    )
    observation = _object_bytes(
        file_raw[one("rig_runtime_observation")["path"]], "rig_runtime_observation.json"
    )
    if request.get("schema_version") != "rig-runtime-request-0.8.0":
        raise ValueError("runtime request schema mismatch")
    if observation.get("schema_version") != "rig-runtime-observation-0.8.0":
        raise ValueError("runtime observation schema mismatch")
    digest = _runtime_request_digest(request)
    if request.get("request_digest") != digest:
        raise ValueError("runtime request request_digest mismatch")
    if observation.get("request_digest") != digest:
        raise ValueError("runtime observation request_digest mismatch")
    manifest_digest = manifest.get("runtime_request_digest")
    if manifest_digest != digest:
        raise ValueError("manifest runtime_request_digest mismatch")
    glb_vertex_count = len(decoded.primitive.positions)
    glb_aff_expected, glb_unaff_expected = _population_from_glb(decoded, contract_data)
    _enforce_runtime_request(
        request,
        contract_data,
        harness_lf,
        vertex_count=glb_vertex_count,
        affected_count=glb_aff_expected,
        unaffected_count=glb_unaff_expected,
    )
    if observation.get("method") != "bake_mesh_from_current_skeleton_pose":
        raise ValueError("runtime observation method mismatch")
    if observation.get("pose_bone") != request.get("pose_bone"):
        raise ValueError("runtime observation pose_bone mismatch")
    obs_godot = _strict_nonempty_string(
        observation.get("godot_version"), "runtime observation godot_version"
    )
    req_godot = _strict_nonempty_string(
        request.get("godot_version"), "runtime request godot_version"
    )
    if obs_godot != req_godot:
        raise ValueError("runtime observation godot_version mismatch")
    for key in ("glb_sha256", "contract_sha256", "harness_sha256"):
        if observation.get(key) != request.get(key):
            raise ValueError(f"runtime binding mismatch on {key}")
    if observation.get("glb_sha256") != glb_entry["sha256"]:
        raise ValueError("observation glb_sha256 does not match bundled GLB")
    if observation.get("contract_sha256") != contract_entry["sha256"]:
        raise ValueError("observation contract_sha256 does not match bundled contract")
    if request.get("glb_sha256") != glb_entry["sha256"]:
        raise ValueError("request glb_sha256 does not match bundled GLB")
    if request.get("contract_sha256") != contract_entry["sha256"]:
        raise ValueError("request contract_sha256 does not match bundled contract")

    _validate_observation_pose(request, observation)

    region_tol = _strict_region_boundary_tolerance(request.get("region_boundary_tolerance"))
    max_aff, max_unaff, aff, unaff = _recompute_runtime(
        observation, decoded, contract_data, boundary_tolerance=region_tol
    )
    if (
        abs(
            _strict_float(observation["max_affected_displacement"], "max_affected_displacement")
            - max_aff
        )
        > 1e-5
    ):
        raise ValueError("max_affected_displacement mismatch")
    if (
        abs(
            _strict_float(observation["max_unaffected_displacement"], "max_unaffected_displacement")
            - max_unaff
        )
        > 1e-5
    ):
        raise ValueError("max_unaffected_displacement mismatch")
    glb_aff, glb_unaff = _population_from_glb(decoded, contract_data)
    if (
        _strict_int(observation.get("affected_vertex_count"), "affected_vertex_count") != aff
        or aff != glb_aff
    ):
        raise ValueError("affected_vertex_count inconsistent with GLB regions")
    if (
        _strict_int(observation.get("unaffected_vertex_count"), "unaffected_vertex_count") != unaff
        or unaff != glb_unaff
    ):
        raise ValueError("unaffected_vertex_count inconsistent with GLB regions")

    oracle = contract_data["deformation_oracle"]
    status = observation.get("status")
    measured_pass = (
        max_aff >= float(oracle["min_affected_displacement"])
        and max_aff <= float(oracle["max_affected_displacement"])
        and max_unaff <= float(oracle["max_unaffected_displacement"])
    )
    if measured_pass != (status == "PASS"):
        raise ValueError("observation status does not match recomputed displacements")
    if status != "PASS":
        raise ValueError("runtime observation must report PASS for verified bundle")
    if manifest.get("runtime_status") != status:
        raise ValueError("manifest runtime_status mismatch")

    source_decl = _object_bytes(
        file_raw[one("source_declaration")["path"]], "source_declaration.json"
    )
    _validate_source_declaration(
        source_decl,
        contract_bytes_hash=contract_bytes_hash,
        contract_canonical_hash=contract_canonical_hash,
    )
    fixture_pin = source_decl.get("positive_fixture_glb_sha256")
    source_claim_matches = isinstance(fixture_pin, str) and fixture_pin == glb_entry["sha256"]

    return {
        "outcome": "CONSISTENT_BUT_UNAUTHENTICATED",
        "integrity_outcome": "VERIFIED",
        "execution_provenance": "CONSISTENT_BUT_UNAUTHENTICATED",
        "reviewed_pins_match": True,
        "source_declaration_fixture_claim_matches_glb": source_claim_matches,
        "validator_contract_version": VALIDATOR_CONTRACT_VERSION,
        "glb_sha256": glb_entry["sha256"],
        "contract_sha256": contract_entry["sha256"],
        "validation_status": recomputed_status,
        "runtime_status": status,
        "verified_files": len(files),
    }


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("usage: python -I verify_rig_bundle.py BUNDLE", file=sys.stderr)
        return 2
    try:
        result = verify_bundle(Path(args[0]))
    except (
        OSError,
        ValueError,
        json.JSONDecodeError,
        UnicodeError,
        InvalidGLB,
        TypeError,
        KeyError,
        IndexError,
        OverflowError,
        RecursionError,
    ) as exc:
        print("FAILED")
        print(json.dumps({"outcome": "FAILED", "error": str(exc)}, ensure_ascii=False))
        return 1
    label = str(result.get("outcome", "FAILED"))
    print(label)
    print(json.dumps(result, sort_keys=True))
    return 0 if label in {"VERIFIED", "CONSISTENT_BUT_UNAUTHENTICATED"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
