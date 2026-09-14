import math
import xml.etree.ElementTree as ET


def parse_junit(data: bytes) -> list[dict]:
    if not data or len(data) > 5_000_000 or b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise ValueError("Missing, oversized or unsafe JUnit XML")
    root = ET.fromstring(data)
    tests = []
    for case in root.iter("testcase"):
        failed = case.find("failure")
        if failed is None:
            failed = case.find("error")
        skipped = case.find("skipped")
        duration = float(case.get("time", "0"))
        if not math.isfinite(duration) or duration < 0:
            raise ValueError("Invalid JUnit duration")
        node = failed if failed is not None else skipped
        tests.append(
            {
                "name": (case.get("classname", "") + "::" + case.get("name", "unnamed"))[-500:],
                "status": "FAILED" if failed is not None else "SKIPPED" if skipped is not None else "PASSED",
                "duration_seconds": duration,
                "message": (
                    (node.get("message", "") + "\n" + (node.text or ""))[:10000] if node is not None else ""
                ),
            }
        )
    if not tests or len(tests) > 10000:
        raise ValueError("Verifier did not provide a supported set of JUnit test cases")
    return tests


def patch_metrics(patch: str) -> dict:
    files = set()
    additions = deletions = 0
    in_hunk = False
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            files.add(line)
            in_hunk = False
        elif line.startswith("@@"):
            in_hunk = True
        elif in_hunk:
            additions += line.startswith("+")
            deletions += line.startswith("-")
    return {"files_changed": len(files), "insertions": additions, "deletions": deletions}
