"""Checks on what the model wrote, because a file existing is not the same as the file being right."""
from __future__ import annotations

import ast
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

_NO_WINDOW = subprocess.CREATE_NO_WINDOW
_C_FAMILY = {".c": ("gcc", "c"), ".h": ("gcc", "c"), ".cpp": ("g++", "c++"), ".cc": ("g++", "c++"), ".cxx": ("g++", "c++"), ".hpp": ("g++", "c++")}
_CODE = {*_C_FAMILY, ".py", ".json", ".js", ".mjs", ".ts", ".java", ".cs", ".go", ".rs", ".sh", ".ps1", ".bat", ".html", ".css", ".sql", ".xml", ".yaml", ".yml", ".php", ".rb"}
_FENCE = re.compile(r"^\s*```[\w+#.-]*[ \t]*\n(.*?)\n?```\s*$", re.DOTALL)
_DIRECTIVE = re.compile(r"^\s*#\s*(?:include|define|undef|if|ifdef|ifndef|elif|else|endif|pragma|error|warning|line)\b")
_MD_HEADING = re.compile(r"^\s*#{1,6}\s+\S")


def is_code(path: Path) -> bool:
    return path.suffix.lower() in _CODE


def tidy_code(path: Path, content: str) -> str:
    """Repairs the habits small models bring from Markdown: fences around the file, and headings in C."""
    fenced = _FENCE.match(content)
    text = fenced.group(1) if fenced else content
    if path.suffix.lower() in _C_FAMILY:
        lines = text.split("\n")
        for index, line in enumerate(lines):
            if _MD_HEADING.match(line) and not _DIRECTIVE.match(line):
                lines[index] = "// " + line.lstrip().lstrip("#").strip()
        text = "\n".join(lines)
    return text


def check_code(path: Path, content: str) -> str:
    """What is wrong with this code, or an empty string. Languages with no checker on this PC pass."""
    suffix = path.suffix.lower()
    if suffix == ".py":
        try:
            ast.parse(content)
        except SyntaxError as exc:
            return f"Line {exc.lineno}: {exc.msg}"
        return ""
    if suffix == ".json":
        try:
            json.loads(content)
        except ValueError as exc:
            return str(exc)
        return ""
    if suffix in _C_FAMILY:
        compiler, language = _C_FAMILY[suffix]
        exe = shutil.which(compiler)
        if not exe:
            return ""
        return _run_checker([exe, "-fsyntax-only", "-x", language, "-Werror=implicit-function-declaration"], path, content, suffix)
    if suffix in {".js", ".mjs"} and shutil.which("node"):
        return _run_checker([shutil.which("node"), "--check"], path, content, suffix)
    return ""


def _run_checker(argv: list[str], path: Path, content: str, suffix: str) -> str:
    with tempfile.TemporaryDirectory(prefix="aegis_check_") as folder:
        target = Path(folder) / f"check{suffix}"
        target.write_text(content, encoding="utf-8")
        try:
            done = subprocess.run([*argv, str(target)], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=25, creationflags=_NO_WINDOW)
        except (OSError, subprocess.SubprocessError):
            return ""
    if done.returncode == 0:
        return ""
    lines = [line.replace(str(target), path.name) for line in done.stderr.splitlines() if "error" in line.lower()]
    return "\n".join(lines[:6])[:900] or done.stderr[:400]


# A sentence about content, rather than content, in a file the user wanted facts in.
_FILLER = re.compile(r"\b(?:this (?:is|file|document) (?:a |an |the )?(?:basic |brief |short )?(?:summary|overview|document|file)\b|based on the information (?:gathered|from|found)|information (?:will|can) be (?:added|found)|for more (?:details|information),? (?:see|visit|please))", re.IGNORECASE)
_WANTS_FACTS = re.compile(r"\b(?:details?|information|info|facts?|about|summary|summari[sz]e|overview|biography|report|history|notes on|explain|guide)\b", re.IGNORECASE)


def looks_like_filler(content: str, request: str) -> bool:
    """A few words that describe a document, when the user asked for what the document should contain."""
    words = len(re.findall(r"\w+", content))
    if words >= 60 or not _WANTS_FACTS.search(request or ""):
        return False
    return words < 25 or bool(_FILLER.search(content))
