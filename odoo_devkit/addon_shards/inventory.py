from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime

_NATIVE = re.compile(r" INFO (\S+) odoo\.addons\.([\w.]+): Starting ([\w.]+) \.\.\.$")
_BROWSER = re.compile(r" INFO (\S+) (odoo\.addons\.[\w.]+)\.browser: (.*)$")
_RESULT = re.compile(r" odoo\.tests\.result: (\d+) failed, (\d+) error\(s\) of (\d+) tests when loading database '([^']+)'$")
_POST_END = re.compile(r" odoo\.service\.server: (\d+) post-tests in ")
_SKIP = re.compile(r" odoo\.(?:addons\.[\w.]+|tests\.result): skipped .+ : ")


@dataclass
class Inventory:
    database: str = ""
    at_install: list[str] = field(default_factory=list)
    post_install: list[str] = field(default_factory=list)
    hoot: list[str] = field(default_factory=list)
    tours: list[str] = field(default_factory=list)
    class_seconds: Counter[str] = field(default_factory=Counter)


def same_inventory(actual: list[str], expected: list[str], label: str) -> None:
    seen, wanted = Counter(actual), Counter(expected)
    if seen != wanted:
        raise ValueError(f"{label}: missing {dict(wanted - seen)}, extra or repeated {dict(seen - wanted)}")


def read_inventory(log: str) -> Inventory:
    inventory = Inventory()
    post = False
    starts = []
    post_counts = []
    summaries = []
    databases = set()
    contexts = []
    successes = []
    passed: dict[str, int] = {}
    counts: Counter[str] = Counter()
    for line in log.splitlines():
        line = line.rstrip()
        if line.endswith(" odoo.service.server: Starting post tests"):
            if post:
                raise ValueError("Repeated post-install phase")
            post = True
        if test := _NATIVE.search(line):
            database, module, method = test.groups()
            databases.add(database)
            name = f"{module}.{method}"
            (inventory.post_install if post else inventory.at_install).append(name)
            if post:
                starts.append((name.rsplit(".", 1)[0], datetime.strptime(line[:23], "%Y-%m-%d %H:%M:%S,%f")))
        if terminal := _POST_END.search(line):
            post_counts.append(int(terminal[1]))
            end = datetime.strptime(line[:23], "%Y-%m-%d %H:%M:%S,%f")
            for (group, start), stop in zip(starts, [t for _, t in starts[1:]] + [end], strict=False):
                inventory.class_seconds[group] += max(0.0, (stop - start).total_seconds())
        if result := _RESULT.search(line):
            summaries.append(result.groups())
        if _SKIP.search(line):
            raise ValueError(f"Skipped native test: {line}")
        if browser := _BROWSER.search(line):
            database, wrapper, message = browser.groups()
            if message.startswith("[HOOT]") or message == "tour succeeded":
                databases.add(database)
            if message == "tour succeeded":
                inventory.tours.append(wrapper)
            elif message == "[HOOT] Starting test suites":
                contexts.append(wrapper)
            elif item := re.fullmatch(r'\[HOOT\] Running test "(.*)"', message):
                counts[wrapper] += 1
                inventory.hoot.append(f"{wrapper} {item[1]}")
            elif total := re.fullmatch(r"\[HOOT\] Passed (\d+) tests? \(.*\)", message):
                if wrapper in passed:
                    raise ValueError(f"Repeated HOOT summary for {wrapper}")
                passed[wrapper] = int(total[1])
            elif message == "[HOOT] Test suite succeeded":
                successes.append(wrapper)
            elif re.match(r"\[HOOT\] (?:Failed|Skipped|Test suite failed)", message, re.I):
                raise ValueError(f"{wrapper}: {message}")
    if not post or post_counts != [len(inventory.post_install)] or len(summaries) != 1:
        raise ValueError("Missing or inconsistent native phase totals")
    failed, errors, count, inventory.database = summaries[0]
    if int(failed) or int(errors) or int(count) != len(inventory.at_install) + len(inventory.post_install):
        raise ValueError(f"Failed or inconsistent native summary: {summaries[0]}")
    if databases - {inventory.database} or dict(counts) != passed:
        raise ValueError("Database or HOOT result differs from its terminal summary")
    same_inventory(contexts, list(counts), "HOOT suites started")
    same_inventory(successes, list(counts), "HOOT suites passed")
    native = Counter(f"odoo.addons.{name}" for name in inventory.at_install + inventory.post_install)
    if any(native[wrapper] != 1 for wrapper in {*contexts, *inventory.tours}):
        raise ValueError("Browser results lack a unique native test")
    return inventory


def split_classes(inventory: Inventory) -> list[list[str]]:
    loads = [0.0, 0.0]
    owners = {}
    for group, seconds in sorted(inventory.class_seconds.items(), key=lambda item: (-item[1], item[0])):
        slot = min(range(2), key=loads.__getitem__)
        owners[group] = slot
        loads[slot] += max(seconds, 0.001)
    shards: list[list[str]] = [[], []]
    for name in inventory.post_install:
        shards[owners[name.rsplit(".", 1)[0]]].append(name)
    if not all(shards):
        raise ValueError("Two nonempty post-install class groups are required")
    return shards
