"""Run owned addon tests in disposable containers, without a runtime target."""

from __future__ import annotations

import argparse
import ast
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Literal, TypedDict

from .addon_shards.inventory import read_inventory, split_classes
from .artifact_inputs import effective_artifact_input_sources, load_artifact_inputs_definition
from .base_python import BASE_PYTHON_PLATFORMS, read_base_python_catalogs
from .dependency_workspace import derive_base_python_lock, stage_publishable_dependency_workspace
from .manifest import WorkspaceManifest, load_workspace_manifest

ROOT = Path(__file__).resolve().parents[1]
SUMMARY = re.compile(r"(\d+) failed, (\d+) error[(]s[)] of (\d+) tests")
DEVTOOLS_PORT_SKIP = re.compile(
    r"\bINFO \S+ odoo\.[\w.]+: "
    r"(skipped [^\n]+? : Failed to detect chrome devtools port after \d+(?:\.\d+)?s\.)$",
    re.MULTILINE,
)
TEST_DATABASE = "addon_ci"


class TestCounts(TypedDict):
    failed: int
    errors: int
    tests: int


class AddonResult(TypedDict, total=False):
    addons: list[str]
    install_addons: list[str]
    installed_addons: list[str]
    external_sources: list[dict[str, str]]
    image: str
    test_image_id: str
    base_image_id: str
    base_image_repo_digests: list[str]
    odoo_version: str
    state: Literal["failed", "passed"]
    failed: int
    errors: int
    tests: int
    seconds: float


class TestRunError(ValueError):
    def __init__(self, message: str, counts: TestCounts) -> None:
        super().__init__(message)
        self.counts = counts


def discover_addons(root: Path) -> list[str]:
    addons = []
    for manifest in sorted(root.glob("*/__manifest__.py")):
        values = ast.literal_eval(manifest.read_text(encoding="utf-8"))
        if values.get("installable", True):
            if not re.fullmatch(r"[A-Za-z0-9_]+", manifest.parent.name):
                raise ValueError(f"Invalid addon directory: {manifest.parent.name}")
            addons.append(manifest.parent.name)
    if not addons:
        raise ValueError(f"No installable addons in {root}")
    return addons


def product_addons_to_install(manifest: WorkspaceManifest) -> set[str]:
    from .local_runtime import (
        LAUNCHPLANE_REQUIRED_ODOO_MODULES,
        load_stack,
        resolve_manifest_runtime_stack_selection,
        resolve_stack_file_path,
    )

    artifact = replace(manifest, runtime=replace(manifest.runtime, instance="artifact"))
    _, selection = resolve_manifest_runtime_stack_selection(
        manifest=artifact,
        stack_definition=load_stack(resolve_stack_file_path(ROOT)).stack_definition,
        artifact_inputs_definition=None,
        runtime_repo_path=ROOT,
        allow_runtime_payload_context=True,
    )
    return {*LAUNCHPLANE_REQUIRED_ODOO_MODULES, *selection.effective_install_modules}


def test_result(log: str, exit_code: int) -> TestCounts:
    matches = SUMMARY.findall(log)
    if not matches:
        raise ValueError("Odoo did not report a test summary")
    failed, errors, count = map(int, matches[-1])
    counts: TestCounts = {"failed": failed, "errors": errors, "tests": count}
    if exit_code or failed or errors or not count:
        raise TestRunError(f"Odoo test run failed: exit={exit_code}, failed={failed}, errors={errors}, tests={count}", counts)
    if skipped := DEVTOOLS_PORT_SKIP.search(log):
        raise TestRunError(f"Incomplete browser coverage: {skipped[1]}", counts)
    return counts


def read_installed_addons(database_container: str) -> list[str]:
    query = "SELECT name FROM ir_module_module WHERE state = 'installed' ORDER BY name"
    return command(
        [
            "docker",
            "exec",
            database_container,
            "psql",
            "-h",
            "127.0.0.1",
            "-U",
            "odoo",
            "-d",
            TEST_DATABASE,
            "-XAt",
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            query,
        ],
        capture=True,
    ).stdout.splitlines()


def execution_environment() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if key != "ADDON_CI_SOURCE_TOKEN"}


def command(args: list[str], *, capture: bool = False, timeout: int = 1800) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, check=True, text=True, capture_output=capture, timeout=timeout, env=execution_environment())


def fetch_source(args: list[str], *, capture: bool = False) -> subprocess.CompletedProcess[str]:
    environment = execution_environment()
    source_token = os.environ.get("ADDON_CI_SOURCE_TOKEN", "")
    if source_token:
        authorization = base64.b64encode(f"x-access-token:{source_token}".encode()).decode()
        environment.update(
            {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
                "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {authorization}",
            }
        )
    # Auth exists only in this Git child's environment, never in argv or checkout config.
    return subprocess.run(args, check=True, text=True, env=environment, capture_output=capture, timeout=180)


def copy_test_inputs(inputs: list[tuple[Path, str]], container: str, output: Path) -> None:
    from .local_runtime import copy_required_path

    with tempfile.TemporaryDirectory(prefix=f"{container}-inputs-", dir=output.parent) as temporary:
        for index, (source, destination) in enumerate(inputs):
            staged = Path(temporary) / str(index)
            excluded = tuple(
                Path(nested).relative_to(destination)
                for _, nested in inputs[index + 1 :]
                if Path(nested).is_relative_to(destination)
            )
            repository = next((path for path in (source, *source.parents) if (path / ".git").exists()), None)
            if repository is not None:
                copy_required_path(
                    repo_path=repository,
                    source_commit=None,
                    source_path=source,
                    destination_path=staged,
                    label="addon CI inputs",
                    exclude_relative_paths=excluded,
                )
            else:
                shutil.copytree(source, staged, ignore=shutil.ignore_patterns(".git", ".venv", ".env", ".env.*", "__pycache__"))
                for relative in excluded:
                    masked = staged / relative
                    if masked.is_symlink():
                        masked.unlink()
                    elif masked.exists():
                        shutil.rmtree(masked)
            staged.mkdir(parents=True, exist_ok=True)
            command(["docker", "cp", f"{staged}/.", f"{container}:{destination}"])


def build_test_image(image: str, tenant: Path | None, context: Path, tag: str, *, support_root: Path | None = None) -> None:
    from .local_runtime import resolve_image_digest, split_image_reference

    repository, _ = split_image_reference(image)
    image = f"{repository}@{resolve_image_digest(image)}"
    base_catalogs = read_base_python_catalogs(image=image, platforms=BASE_PYTHON_PLATFORMS)
    catalogs = []
    for name in ("runtime-python", "addon-tests"):
        staged = context / name
        staged.mkdir()
        for filename in ("pyproject.toml", "uv.lock"):
            shutil.copy2(ROOT / "docker" / name / filename, staged / filename)
        catalogs.append(staged)
    manifest = load_workspace_manifest(tenant / "workspace.toml") if tenant is not None else None
    python_requirement = (
        manifest.workspace.python_version
        if manifest is not None
        else tomllib.loads((catalogs[1] / "pyproject.toml").read_text())["project"]["requires-python"]
    )
    command(["uv", "python", "install", python_requirement])
    if manifest is not None:
        # Actions checkouts need not use the developer's sibling directory names.
        manifest = replace(
            manifest,
            devkit_repo=replace(manifest.devkit_repo, path=str(ROOT)) if manifest.devkit_repo else None,
            shared_addons_repo=(
                replace(manifest.shared_addons_repo, path=str(support_root))
                if manifest.shared_addons_repo and support_root is not None
                else manifest.shared_addons_repo
            ),
        )
        staged = context / "tenant"
        stage_publishable_dependency_workspace(manifest=manifest, destination_root=staged, committed=False)
        catalogs.append(staged)
    requirements = []
    for catalog in catalogs:
        derive_base_python_lock(staged_root=catalog, catalogs=base_catalogs, python_version=python_requirement)
        command(["uv", "lock", "--project", str(catalog), "--python", python_requirement, "--check", "--offline", "--no-config"])
        requirements.append(
            command(
                [
                    "uv",
                    "export",
                    "--project",
                    str(catalog),
                    "--frozen",
                    "--all-packages",
                    "--no-emit-workspace",
                    "--no-default-groups",
                    "--no-config",
                    "--no-header",
                ],
                capture=True,
            ).stdout
        )
    (context / "requirements.txt").write_text("\n".join(requirements), encoding="utf-8")
    (context / ".dockerignore").write_text("external/**/.git\n", encoding="utf-8")
    (context / "Dockerfile").write_text(
        "ARG BASE_IMAGE\nFROM ${BASE_IMAGE}\nUSER root\n"
        "COPY requirements.txt /tmp/addon-ci-requirements.txt\n"
        "COPY external /opt/ci-external\n"
        "RUN uv pip freeze --python /venv/bin/python > /tmp/base-constraints.txt "
        "&& uv pip install --python /venv/bin/python --no-deps "
        "--constraint /tmp/base-constraints.txt -r /tmp/addon-ci-requirements.txt "
        "&& uv pip check --python /venv/bin/python\nUSER ubuntu\n",
        encoding="utf-8",
    )
    command(["docker", "build", "--build-arg", f"BASE_IMAGE={image}", "--tag", tag, str(context)])


def parallel_reference_payload(reference: Path, result: AddonResult) -> dict:
    serial = json.loads((reference / "result.json").read_text())
    for key in ("addons", "install_addons", "odoo_version", "image", "external_sources"):
        if serial.get(key) != result[key]:
            raise ValueError(f"Serial reference does not match this run's {key}")
    log = (reference / "odoo.log").read_text()
    counts = test_result(log, 0)
    if serial["state"] != "passed" or any(serial[key] != value for key, value in counts.items()):
        raise ValueError("Serial reference did not pass with the recorded test counts")
    inventory = read_inventory(log)
    split_classes(inventory)
    return {"installed_addons": serial["installed_addons"], "inventory": vars(inventory)}


def stage_parallel_tests(payload: dict, container: str, output: Path) -> None:
    with tempfile.TemporaryDirectory(dir=output.parent) as temp:
        staged = Path(temp)
        scripts = staged / "ci-runner"
        scripts.mkdir()
        scripts.chmod(0o755)
        package = scripts / "addon_shards"
        package.mkdir()
        package.chmod(0o755)
        for filename in ("__init__.py", "inventory.py", "runner.py", "suite.py"):
            destination = package / filename
            shutil.copyfile(Path(__file__).with_name("addon_shards") / filename, destination)
            destination.chmod(0o755 if filename == "suite.py" else 0o644)
        reference_file = scripts / "reference.json"
        reference_file.write_text(json.dumps(payload))
        reference_file.chmod(0o644)
        command(["docker", "cp", str(scripts), f"{container}:/opt/ci-runner"])


def run_tests(
    *,
    image: str,
    version: str,
    addons_root: Path,
    support_root: Path | None,
    tenant: Path | None,
    output: Path,
    timeout: int,
    devkit_checks: bool = False,
    parallel_reference: Path | None = None,
) -> None:
    if parallel_reference is not None and output.exists() and any(output.iterdir()):
        raise ValueError("Use an empty output directory for parallel evidence, separate from the serial reference")
    owned_addons = discover_addons(addons_root)
    install_addons = set(owned_addons)
    token = f"odoo-addon-ci-{uuid.uuid4().hex[:12]}"
    database_container = f"{token}-db"
    test_container = f"{token}-tests"
    image_tag = f"{token}:test"
    started = time.monotonic()
    output.mkdir(parents=True, exist_ok=True)
    result: AddonResult = {"addons": owned_addons, "image": image, "odoo_version": version, "state": "failed"}
    external_sources: list[dict[str, str]] = []
    external_paths: list[str] = []
    result["external_sources"] = external_sources
    try:
        manifest = None
        if tenant is not None:
            manifest = load_workspace_manifest(tenant / "workspace.toml")
            product_addons = product_addons_to_install(manifest)
            if addons_root != tenant / "addons":
                available_support = discover_addons(support_root) if support_root is not None else ()
                product_addons.intersection_update(available_support)
            install_addons.update(product_addons)
        elif support_root is not None:
            install_addons.update(discover_addons(support_root))
        result["install_addons"] = sorted(install_addons)
        with tempfile.TemporaryDirectory(prefix=token, dir=output.parent) as temporary:
            context = Path(temporary)
            external_root = context / "external"
            external_root.mkdir()
            if manifest is not None:
                inputs = load_artifact_inputs_definition(manifest=manifest)
                if inputs:
                    sources = effective_artifact_input_sources(
                        artifact_inputs_definition=inputs, context_name=manifest.runtime.context, instance_name="local"
                    )
                    for source in sources:
                        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", source.repository):
                            raise ValueError("Addon CI requires a GitHub owner/repository source")
                        checkout = external_root / source.repository.split("/")[1]
                        command(["git", "init", "--quiet", str(checkout)])
                        command(
                            ["git", "-C", str(checkout), "remote", "add", "origin", f"https://github.com/{source.repository}.git"]
                        )
                        fetch_source(
                            [
                                "git",
                                "-C",
                                str(checkout),
                                "fetch",
                                "--depth",
                                "1",
                                "origin",
                                "--",
                                source.exact_ref or source.selector or "HEAD",
                            ]
                        )
                        command(["git", "-C", str(checkout), "checkout", "--detach", "FETCH_HEAD"])
                        source_commit = command(["git", "-C", str(checkout), "rev-parse", "HEAD"], capture=True).stdout.strip()
                        external_sources.append({"repository": source.repository, "commit": source_commit})
                        if (checkout / "__manifest__.py").is_file():
                            external_paths.append("/opt/ci-external")
                        elif (checkout / "addons").is_dir():
                            external_paths.append(f"/opt/ci-external/{checkout.name}/addons")
                        else:
                            external_paths.append(f"/opt/ci-external/{checkout.name}")
            build_image = image
            if devkit_checks:
                inspect_image = ["docker", "image", "inspect", image]
                try:
                    base_metadata = json.loads(command(inspect_image, capture=True).stdout)[0]
                except subprocess.CalledProcessError:
                    command(["docker", "pull", image])
                    base_metadata = json.loads(command(inspect_image, capture=True).stdout)[0]
                result["base_image_id"] = base_metadata["Id"]
                result["base_image_repo_digests"] = base_metadata["RepoDigests"]
                if result["base_image_repo_digests"]:
                    build_image = result["base_image_repo_digests"][0]
            build_test_image(build_image, tenant, context, image_tag, support_root=support_root)
        result["test_image_id"] = command(
            ["docker", "image", "inspect", image_tag, "--format", "{{.Id}}"], capture=True
        ).stdout.strip()
        actual_version = command(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--env",
                "ODOO_BROWSER_BIN=/usr/local/bin/chromium-playwright",
                "--entrypoint",
                "/venv/bin/python",
                image_tag,
                "-c",
                "import odoo.release; import websocket; import subprocess; "
                "from odoo.tests.common import _find_executable; "
                "subprocess.run([_find_executable(), '--version'], check=True, capture_output=True); "
                "print('.'.join(map(str, odoo.release.version_info[:2])))",
            ],
            capture=True,
        ).stdout.strip()
        if not actual_version.startswith(version + ".") and actual_version != version:
            raise ValueError(f"Expected Odoo {version}, image has {actual_version}")
        command(["docker", "network", "create", "--internal", token])
        command(
            [
                "docker",
                "run",
                "--detach",
                "--name",
                database_container,
                "--network",
                token,
                "--network-alias",
                "database",
                "--tmpfs",
                "/var/lib/postgresql/data",
                "--env",
                "POSTGRES_USER=odoo",
                "--env",
                "POSTGRES_DB=postgres",
                "--env",
                "POSTGRES_HOST_AUTH_METHOD=trust",
                "postgres:17",
            ]
        )
        ready_by = time.monotonic() + 60
        while subprocess.run(
            ["docker", "exec", database_container, "pg_isready", "-h", "127.0.0.1", "-U", "odoo"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=execution_environment(),
        ).returncode:
            if time.monotonic() >= ready_by:
                raise TimeoutError("Throwaway PostgreSQL did not become ready")
            time.sleep(1)
        inputs = [(addons_root, "/opt/project/addons")]
        paths = ["/opt/project/addons", "/opt/extra_addons", "/opt/enterprise", "/odoo/addons", "/odoo/odoo/addons"]
        paths.extend(sorted(set(external_paths)))
        python_paths = ["/odoo", "/opt/project/addons"]
        if tenant is not None:
            inputs.insert(0, (tenant, "/opt/project"))
        if support_root is not None:
            support_path = "/opt/support-addons" if devkit_checks else "/opt/extra_addons"
            inputs.append((support_root, support_path))
            if support_path not in paths:
                paths.append(support_path)
            python_paths.append(support_path)
        if devkit_checks:
            inputs.append((ROOT / "docker/scripts", "/volumes/scripts"))
            inputs.append((ROOT / "tests/live_odoo/extra_addons", "/opt/extra_addons"))
            paths.append("/opt/extra_addons/ci_enterprise")
            python_paths.insert(0, "/volumes/scripts")
        mounts = [argument for _, destination in inputs for argument in ("--mount", f"type=volume,dst={destination},volume-nocopy")]
        program = ["/odoo/odoo-bin", image_tag]
        if parallel_reference is not None:
            python_paths.append("/opt/ci-runner")
            program = ["/venv/bin/python", image_tag, "-m", "addon_shards.runner"]
        test_command = [
            "docker",
            "create",
            "--name",
            test_container,
            "--network",
            token,
            "--shm-size",
            "2g",
            *mounts,
            "--env",
            f"PYTHONPATH={':'.join(python_paths)}",
            "--env",
            "PLATFORM_INSTANCE=local",
            "--env",
            "ODOO_BROWSER_BIN=/usr/local/bin/chromium-playwright",
            "--entrypoint",
            *program,
            "--db_host",
            "database",
            "--db_user",
            "odoo",
            "--database",
            TEST_DATABASE,
            "--addons-path",
            ",".join(paths),
            "--data-dir",
            "/tmp/odoo-data",
            "--without-demo",
            "all",
            "--init",
            ",".join(result["install_addons"]),
            "--test-enable",
            "--test-tags",
            ",".join(f"/{name}" for name in owned_addons),
            "--stop-after-init",
            "--max-cron-threads",
            "0",
            "--workers",
            "0",
            "--limit-time-real",
            str(timeout),
        ]
        command(test_command)
        copy_test_inputs(inputs, test_container, output)
        if parallel_reference is not None:
            (output / "shards").mkdir()
            stage_parallel_tests(parallel_reference_payload(parallel_reference, result), test_container, output)
        with (output / "odoo.log").open("w", encoding="utf-8") as log_file:
            try:
                completed = subprocess.run(
                    ["docker", "start", "--attach", test_container],
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    text=True,
                    timeout=timeout,
                    env=execution_environment(),
                )
            except subprocess.TimeoutExpired:
                if parallel_reference is not None:
                    log_file.write(f"Shared addon deadline expired after {timeout} seconds\n")
                    subprocess.run(["docker", "kill", test_container], capture_output=True, timeout=30, env=execution_environment())
                raise
            finally:
                if parallel_reference is not None:
                    collected = subprocess.run(
                        ["docker", "cp", f"{test_container}:/tmp/addon-results/.", str(output / "shards")],
                        capture_output=True,
                        text=True,
                        timeout=60,
                        env=execution_environment(),
                    )
                    if collected.returncode:
                        log_file.write(f"Could not collect addon shard evidence: {collected.stderr}\n")
                        if sys.exc_info()[0] is None and completed.returncode == 0:
                            collected.check_returncode()
        log = (output / "odoo.log").read_text(encoding="utf-8")
        print(log)
        if parallel_reference is None:
            result.update(test_result(log, completed.returncode))
        elif completed.returncode:
            raise ValueError(f"Parallel addon validation exited {completed.returncode}; see the shard logs")
        else:
            count = json.loads((output / "shards/counts.json").read_text())["tests"]
            if type(count) is not int or count <= 0:
                raise ValueError("No completed native tests in the parallel result")
            result.update(failed=0, errors=0, tests=count)
        result["installed_addons"] = read_installed_addons(database_container)
        missing = sorted(install_addons.difference(result["installed_addons"]))
        if missing:
            raise ValueError(f"Odoo did not install required addons: {', '.join(missing)}")
        result["state"] = "passed"
    except TestRunError as error:
        result.update(error.counts)
        raise
    finally:
        # Names are generated for this invocation; never enumerate or prune other Docker resources.
        cleanup_failed = []
        for args in (
            ["docker", "rm", "--force", "--volumes", test_container, database_container],
            ["docker", "network", "rm", token],
            ["docker", "image", "rm", image_tag],
        ):
            try:
                removed = subprocess.run(
                    args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60, env=execution_environment()
                )
            except subprocess.TimeoutExpired:
                if parallel_reference is None:
                    raise
                cleanup_failed.append(args)
                continue
            if parallel_reference is not None and removed.returncode:
                cleanup_failed.append(args)
        if cleanup_failed:
            result["state"] = "failed"
        result["seconds"] = round(time.monotonic() - started, 1)
        (output / "result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        summary = f"Addon CI: {result['state']}, {result.get('tests', 'unknown')} tests, {result['seconds']} seconds\n"
        print(summary)
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as summary_file:
                summary_file.write(summary)
        if cleanup_failed and sys.exc_info()[0] is None:
            raise RuntimeError(f"Failed to remove disposable addon resources: {cleanup_failed}")


def argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image")
    parser.add_argument("--odoo-version")
    parser.add_argument("--addons-root", type=Path, required=True)
    parser.add_argument("--support-root", type=Path)
    parser.add_argument("--tenant", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--parallel-reference", type=Path, help="Run two post-install workers against this passing serial evidence directory"
    )
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--devkit-checks", action="store_true", help="Copy devkit scripts and synthetic live-check addons")
    return parser


def main() -> None:
    parser = argument_parser()
    arguments = parser.parse_args()
    if arguments.tenant:
        manifest = load_workspace_manifest(arguments.tenant.resolve() / "workspace.toml")
        arguments.image = arguments.image or manifest.build.base_devtools_image
        arguments.odoo_version = arguments.odoo_version or manifest.build.odoo_version
    if not arguments.image:
        parser.error("--image is required without a tenant manifest")
    if not arguments.odoo_version:
        first_manifest = arguments.addons_root / discover_addons(arguments.addons_root)[0] / "__manifest__.py"
        addon_version = ast.literal_eval(first_manifest.read_text())["version"]
        arguments.odoo_version = ".".join(addon_version.split(".")[:2])
    run_tests(
        image=arguments.image,
        version=arguments.odoo_version,
        addons_root=arguments.addons_root.resolve(),
        support_root=arguments.support_root.resolve() if arguments.support_root else None,
        tenant=arguments.tenant.resolve() if arguments.tenant else None,
        output=arguments.output.resolve(),
        timeout=arguments.timeout,
        devkit_checks=arguments.devkit_checks,
        parallel_reference=arguments.parallel_reference,
    )


if __name__ == "__main__":
    main()
