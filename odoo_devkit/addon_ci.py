"""Run owned addon tests in disposable containers, without a runtime target."""

from __future__ import annotations

import argparse
import ast
import base64
import json
import os
import re
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Literal, TypedDict

from .artifact_inputs import effective_artifact_input_sources, load_artifact_inputs_definition
from .manifest import load_workspace_manifest

ROOT = Path(__file__).resolve().parents[1]
SUMMARY = re.compile(r"(\d+) failed, (\d+) error[(]s[)] of (\d+) tests")


class TestCounts(TypedDict):
    failed: int
    errors: int
    tests: int


class AddonResult(TypedDict, total=False):
    addons: list[str]
    external_sources: list[dict[str, str]]
    image: str
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


def test_result(log: str, exit_code: int) -> TestCounts:
    matches = SUMMARY.findall(log)
    if not matches:
        raise ValueError("Odoo did not report a test summary")
    failed, errors, count = map(int, matches[-1])
    counts: TestCounts = {"failed": failed, "errors": errors, "tests": count}
    if exit_code or failed or errors or not count:
        raise TestRunError(f"Odoo test run failed: exit={exit_code}, failed={failed}, errors={errors}, tests={count}", counts)
    return counts


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


def build_test_image(image: str, tenant: Path | None, context: Path, tag: str) -> None:
    catalogs = [ROOT / "docker" / "runtime-python", ROOT / "docker" / "addon-tests"]
    if tenant is not None:
        catalogs.append(tenant)
    requirements = []
    for catalog in catalogs:
        command(["uv", "lock", "--project", str(catalog), "--check", "--offline", "--no-config"])
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


def run_tests(
    *, image: str, version: str, addons_root: Path, support_root: Path | None, tenant: Path | None, output: Path, timeout: int
) -> None:
    owned_addons = discover_addons(addons_root)
    install_addons = list(owned_addons)
    if support_root is not None:
        install_addons = sorted(set(install_addons + discover_addons(support_root)))
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
        with tempfile.TemporaryDirectory(prefix=token, dir=output) as temporary:
            context = Path(temporary)
            external_root = context / "external"
            external_root.mkdir()
            if tenant is not None:
                manifest = load_workspace_manifest(tenant / "workspace.toml")
                inputs = load_artifact_inputs_definition(manifest=manifest)
                if inputs:
                    sources = effective_artifact_input_sources(
                        artifact_inputs_definition=inputs, context_name=manifest.runtime.context, instance_name="local"
                    )
                    for source in sources:
                        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", source.repository):
                            raise ValueError("Addon CI requires a GitHub owner/repository source")
                        checkout = external_root / source.repository.split("/")[1]
                        fetch_source(["git", "clone", "--no-checkout", f"https://github.com/{source.repository}.git", str(checkout)])
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
            build_test_image(image, tenant, context, image_tag)
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
        ).returncode:
            if time.monotonic() >= ready_by:
                raise TimeoutError("Throwaway PostgreSQL did not become ready")
            time.sleep(1)
        mounts = ["--volume", f"{addons_root}:/opt/project/addons:ro"]
        paths = ["/opt/project/addons", "/opt/extra_addons", "/opt/enterprise", "/odoo/addons", "/odoo/odoo/addons"]
        paths.extend(sorted(set(external_paths)))
        python_paths = ["/odoo", "/opt/project/addons"]
        if tenant is not None:
            mounts = ["--volume", f"{tenant}:/opt/project:ro", *mounts]
        if support_root is not None:
            mounts.extend(["--volume", f"{support_root}:/opt/extra_addons:ro"])
            python_paths.append("/opt/extra_addons")
        test_command = [
            "docker",
            "run",
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
            "/odoo/odoo-bin",
            image_tag,
            "--db_host",
            "database",
            "--db_user",
            "odoo",
            "--database",
            "addon_ci",
            "--addons-path",
            ",".join(paths),
            "--data-dir",
            "/tmp/odoo-data",
            "--without-demo",
            "all",
            "--init",
            ",".join(install_addons),
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
        with (output / "odoo.log").open("w", encoding="utf-8") as log_file:
            completed = subprocess.run(
                test_command, stdout=log_file, stderr=subprocess.STDOUT, text=True, timeout=timeout, env=execution_environment()
            )
        log = (output / "odoo.log").read_text(encoding="utf-8")
        print(log)
        result.update(test_result(log, completed.returncode))
        result["state"] = "passed"
    except TestRunError as error:
        result.update(error.counts)
        raise
    finally:
        # Names are generated for this invocation; never enumerate or prune other Docker resources.
        for args in (
            ["docker", "rm", "--force", test_container, database_container],
            ["docker", "network", "rm", token],
            ["docker", "image", "rm", image_tag],
        ):
            subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60, env=execution_environment())
        result["seconds"] = round(time.monotonic() - started, 1)
        (output / "result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        summary = f"Addon CI: {result['state']}, {result.get('tests', 'unknown')} tests, {result['seconds']} seconds\n"
        print(summary)
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as summary_file:
                summary_file.write(summary)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image")
    parser.add_argument("--odoo-version")
    parser.add_argument("--addons-root", type=Path, required=True)
    parser.add_argument("--support-root", type=Path)
    parser.add_argument("--tenant", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=1800)
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
    )


if __name__ == "__main__":
    main()
