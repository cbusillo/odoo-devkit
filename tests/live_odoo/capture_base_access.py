from __future__ import annotations

import csv
import hashlib
import io
import json
import sys
from pathlib import Path


def capture_contract(capture: Path, qualification: Path, output: Path) -> None:
    receipt = json.loads(qualification.read_text())
    digest = hashlib.sha256(capture.read_bytes()).hexdigest()
    if receipt["status"] != "qualified_native19_factory_declaration_capture" or digest != receipt["declaration_sha256"]:
        raise RuntimeError("The native19 factory capture has no matching independent qualification")
    native = json.loads(capture.read_text())
    groups: dict[int, list[str]] = {}
    for _identity, module, name, model, identifier, _protected in native["identities"]:
        if module == "base" and model == "res.groups":
            groups.setdefault(identifier, []).append(f"{module}.{name}")
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(
        ("source_model", "xmlid", "name", "model", "groups", "operation", "domain", "active", "noupdate", "delegated_parents")
    )
    for policy in native["policies"]:
        aliases = [alias for alias in policy["alias_records"] if alias["module"] == "base"]
        if not aliases:
            continue
        if len(aliases) != 1 or any(len(groups.get(identifier, [])) != 1 for identifier in policy["groups"]):
            raise RuntimeError("The native19 base policy has ambiguous policy/group identities")
        alias = aliases[0]
        writer.writerow(
            (
                policy["source_model"],
                f"base.{alias['name']}",
                policy["name"],
                policy["model"],
                ";".join(sorted(groups[identifier][0] for identifier in policy["groups"])),
                policy["operation"],
                policy["domain"],
                int(policy["active"]),
                int(alias["noupdate"]),
                ";".join(sorted(policy["delegated_parents"])),
            )
        )
    content = stream.getvalue().encode()
    provenance = {
        "capture_sha256": digest,
        "artifact_sha256": hashlib.sha256(content).hexdigest(),
        "packet_sha256": receipt["packet_sha256"],
        "job_id": receipt["job_id"],
        "upstream_commit": None,
        "upstream_commit_status": "Not captured; identity is bound by the independently qualified immutable factory packet",
    }
    output.write_bytes(content)
    output.with_suffix(".provenance.json").write_text(json.dumps(provenance, indent=2) + "\n")


if __name__ == "__main__":
    capture_contract(*(Path(argument) for argument in sys.argv[1:]))
