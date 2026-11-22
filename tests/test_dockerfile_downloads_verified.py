"""Static check: the image build verifies every download before it uses it.

A `RUN` that downloads with `curl` or `wget` must, in that same `RUN`, verify the file it
wrote. Two forms count, both after the download:

* `echo "<hash>  <file>" | sha256sum -c -`, with `set -e` in force so a mismatch stops the
  build. The line that feeds the check must name the downloaded file.
* A hash computed from the file with `sha256sum <file>` into a variable, then a compare
  of that variable with an expected value that exits non-zero on a mismatch.

These fail: no comparison (a bare `sha256sum <file>` only prints), a check of a different
file, a check in a different `RUN`, a failure that is ignored with `|| true`, and any
`ADD` from a URL or download piped into a shell.

Fix a failure by downloading to a file, comparing its hash with a pinned value, and
failing on a mismatch, then using the file.
"""
# ruff: noqa: E501  (the patterns and sample Dockerfile lines read best on one line)
import re
from pathlib import Path

DOCKERFILE = Path(__file__).resolve().parent.parent / "Dockerfile"
FETCH = re.compile(r"(?<![\w-])(?:curl|wget)\s+(?:-|[\"']|https?:)[^;&|\n]*")
CURL_OUT = re.compile(r"\s(?:-[A-Za-z]*o|--output)\s+(\"[^\"]+\"|'[^']+'|[^\s;&|]+)")
WGET_OUT = re.compile(r"\s(?:-[A-Za-z]*O|--output-document)\s+(\"[^\"]+\"|'[^']+'|[^\s;&|]+)")
PIPED_INTO_SHELL = re.compile(r"\b(?:curl|wget)\b[^|]*\|\s*(?:sudo\s+)?(?:ba|da)?sh\b")
REMOTE_ADD = re.compile(r"^\s*ADD\s+(?:--\S+\s+)*https?://", re.I | re.M)
SET_E = re.compile(r"\bset\s+-[a-z]*e")
STOP = r"(?:exit|return)\s+[1-9]"


def runs_in(text):
    """Return each RUN instruction, with continuation lines joined and comments dropped."""
    lines = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
    joined = re.sub(r"\\\n\s*", " ", "\n".join(lines))
    return [line for line in joined.splitlines() if re.match(r"\s*RUN\b", line, re.I)], joined


def tokens(text):
    """Split shell text into words, quotes removed, so paths compare as whole words."""
    return [t for t in re.split(r"[\s;&|()<>\"']+", text) if t]


HASH_STEP = re.compile(r"(\w+)=\"?\$\(\s*(?:sha256sum|shasum\s+-a\s+256)\s+([^\s)|]+)[^)]*\)\"?")


def verification_start(run, target, after):
    """Return where the RUN stops on a hash mismatch for `target` after `after`, or None.

    For `sha256sum -c` that is the check. For a computed hash it is the compare that exits,
    not the hashing step, so the file stays unverified until the compare runs.
    """
    target = target.strip("\"'")
    for found in re.finditer(r"(?:echo|printf)\s([^|;&]*)\|\s*sha256sum\s+(?:-c|--check)\b(?![^;&]*\|\|\s*(?:true|:))", run[after:]):
        end = after + found.end()
        chained = run[end:].lstrip().startswith("&&") or run[: after + found.start()].rstrip().endswith("&&")
        if target in tokens(found.group(1)) and (SET_E.search(run) or chained):
            return after + found.start()
    for var in HASH_STEP.finditer(run, after):
        v, rest = var.group(1), run[var.end() :]
        if var.group(2).strip("\"'") != target:
            continue
        for compare in (
            rf"\[\s+\"\${{?{v}}}?\"\s+!=\s+[^\]]+\]\s*;\s*then\s+(?:[^;]*;\s*)*?{STOP}",
            rf"\[\s+\"\${{?{v}}}?\"\s+=\s+[^\]]+\]\s*\|\|\s*{STOP}",
        ):
            found = re.search(compare, rest)
            if found:
                return var.end() + found.start()
    return None


def problems_in(text):
    runs, joined = runs_in(text)
    found = [f"ADD from a URL: {m.group(0).strip()}" for m in REMOTE_ADD.finditer(joined)]
    for run in runs:
        if PIPED_INTO_SHELL.search(run):
            found.append(f"download piped into a shell: {run.strip()[:80]}")
            continue
        # Every download is judged on its own, so one verified download covers no other.
        for download in FETCH.finditer(run):
            outputs = list((WGET_OUT if download.group(0).startswith("wget") else CURL_OUT).finditer(download.group(0)))
            if not outputs:
                found.append(f"download with no output file to verify: {download.group(0)[:80]}")
            for out in outputs:
                target, end = out.group(1), download.end()
                start = verification_start(run, target, end)
                if start is None:
                    found.append(f"download of {target} is not verified in the same RUN")
                elif target.strip("\"'") in tokens(HASH_STEP.sub(" ", run[end:start])):
                    found.append(f"{target} is used before it is verified")
    return found


def test_dockerfile_downloads_are_verified():
    assert problems_in(DOCKERFILE.read_text(encoding="utf-8")) == []


GOOD_COMPARE = (
    'RUN set -eux; fetch() { curl -fsSLo "$2" "$1"; actual="$(sha256sum "$2" | cut -d" " -f1)"; '
    'if [ "$actual" != "$3" ]; then echo "mismatch" >&2; exit 1; fi; }; fetch https://e.test/a /tmp/a 00\n'
)
GOOD_CHECK = 'RUN set -eux; curl -fsSLo /tmp/a https://e.test/a; echo "00  /tmp/a" | sha256sum -c -; chmod +x /tmp/a\n'


def test_verified_downloads_pass():
    assert problems_in(GOOD_COMPARE) == []
    assert problems_in(GOOD_CHECK) == []
    assert problems_in("RUN apt-get install -y --no-install-recommends curl git\n") == []


def test_planted_downloads_are_caught():
    planted = {
        "ADD from a URL": "ADD https://e.test/a.tgz /tmp/\n",
        "curl piped into sh": "RUN curl -fsSL https://e.test/i.sh | sh\n",
        "no comparison at all": "RUN curl -fsSLo /tmp/a https://e.test/a && chmod +x /tmp/a\n",
        "hash only printed": "RUN curl -fsSLo /tmp/a https://e.test/a && sha256sum /tmp/a && sh /tmp/a\n",
        "check names a different file": 'RUN set -e; curl -fsSLo /tmp/a https://e.test/a; echo "00  /tmp/b" | sha256sum -c -\n',
        "compare of a different file": GOOD_COMPARE.replace('sha256sum "$2"', "sha256sum /tmp/other"),
        "compare never exits": GOOD_COMPARE.replace("exit 1", "true"),
        "check failure ignored": GOOD_CHECK.replace("sha256sum -c -", "sha256sum -c - || true"),
        "check without set -e": GOOD_CHECK.replace("set -eux; ", ""),
        "download with no output file": "RUN curl -fsSL https://e.test/a\n",
        "wget to a file, no check": "RUN wget -qO /tmp/a https://e.test/a\n",
        "check in a different RUN": GOOD_CHECK.split("; echo")[0] + "\nRUN echo '00  /tmp/a' | sha256sum -c -\n",
        "used before the check": 'RUN set -e; curl -fsSLo /tmp/a https://e.test/a; sh /tmp/a; echo "00  /tmp/a" | sha256sum -c -\n',
        "check names a longer path": 'RUN set -e; curl -fsSLo /tmp/a https://e.test/a; echo "00  /tmp/abc" | sha256sum -c -; sh /tmp/a\n',
        "check names a backup path": 'RUN set -e; curl -fsSLo /tmp/a https://e.test/a; echo "00  /tmp/a.bak" | sha256sum -c -; sh /tmp/a\n',
        "compare of a longer path": GOOD_COMPARE.replace('sha256sum "$2"', 'sha256sum "${2}x"').replace('curl -fsSLo "$2"', 'curl -fsSLo /tmp/a').replace(
            "fetch https://e.test/a /tmp/a 00", ""
        ),
        "used between the hash and the compare": 'RUN set -e; curl -fsSLo /tmp/a https://e.test/a; h="$(sha256sum /tmp/a)"; sh /tmp/a; if [ "$h" != "00" ]; then exit 1; fi\n',
        "check names a tilde path": 'RUN set -e; curl -fsSLo /tmp/a https://e.test/a; echo "00  /tmp/a~" | sha256sum -c -; sh /tmp/a\n',
        "check names a directory path": 'RUN set -e; curl -fsSLo /tmp/a https://e.test/a; echo "00  /tmp/a/" | sha256sum -c -; sh /tmp/a\n',
        "verified download then an unverified one": GOOD_CHECK.rstrip("\n") + "; curl -fsSLO https://e.test/tool\n",
        "verified download then a second unverified output": GOOD_CHECK.rstrip("\n") + "; curl -fsSLo /tmp/b https://e.test/b\n",
    }
    for name, text in planted.items():
        assert problems_in(text), name
