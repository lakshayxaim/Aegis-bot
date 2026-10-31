"""Exact arithmetic, and the escape hatch: running code the model wrote, only after the user approves it."""
from __future__ import annotations

import ast
import logging
import math
import operator
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from aegis import config, netguard, paths
from aegis.toolkit import DANGER, Approval, Result, ToolError, arg, resolve_input, tool

logger = logging.getLogger("aegis.code")
_NO_WINDOW = subprocess.CREATE_NO_WINDOW

_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow, ast.USub: operator.neg, ast.UAdd: operator.pos}
_FUNCS = {name: getattr(math, name) for name in ("sqrt", "sin", "cos", "tan", "asin", "acos", "atan", "log", "log10", "log2", "exp", "floor", "ceil", "factorial", "gcd", "radians", "degrees")}
_FUNCS.update({"abs": abs, "round": round, "min": min, "max": max, "sum": lambda *values: sum(values)})
_CONSTS = {"pi": math.pi, "e": math.e, "tau": math.tau}


def _eval(node: ast.AST) -> float:
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        left, right = _eval(node.left), _eval(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > 4096:
            raise ToolError("That exponent is too large.")
        return _OPS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.operand))
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCS and not node.keywords:
        return _FUNCS[node.func.id](*[_eval(item) for item in node.args])
    if isinstance(node, ast.Name) and node.id in _CONSTS:
        return _CONSTS[node.id]
    raise ToolError("Only arithmetic is allowed here, for example 17 * 2350 / 100 or sqrt(2).")


def _tidy(value: float) -> str:
    if isinstance(value, float) and value.is_integer() and abs(value) < 1e15:
        return f"{int(value):,}"
    if isinstance(value, float):
        return f"{value:,.10g}"
    return f"{value:,}"


@tool("calculate", "Work out arithmetic exactly. Always use this for percentages, sums, conversions and any other maths.", group="Maths & code", doing="Calculating",
      keywords="calculate compute math maths sum percent percentage plus minus times divide multiply add subtract how much total tip square root equals",
      expression=arg("string", "An arithmetic expression, e.g. 17 * 2350 / 100", required=True))
def calculate(ctx, expression: str) -> Result:
    text = expression.replace("^", "**").replace("×", "*").replace("÷", "/").replace(",", "")
    text = text.split("=")[0].strip()
    try:
        tree = ast.parse(text, mode="eval")
        value = _eval(tree)
    except SyntaxError as exc:
        raise ToolError(f"'{expression}' is not valid arithmetic.", "Write it with numbers and operators, e.g. 17 * 2350 / 100.") from exc
    except ZeroDivisionError as exc:
        raise ToolError("That divides by zero.") from exc
    except (OverflowError, ValueError, TypeError) as exc:
        raise ToolError(f"Could not work that out: {exc}") from exc
    return Result(f"{expression.strip()} = {_tidy(value)}", verified="Computed exactly; no model estimate involved.")


def _run(ctx, argv: list[str], *, cwd: Path, timeout: int, env: dict[str, str] | None = None) -> tuple[int, str, str]:
    process = subprocess.Popen(argv, cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", env=env, creationflags=_NO_WINDOW)
    ctx.procs.append(process)
    out: list[str] = []
    err: list[str] = []
    readers = [threading.Thread(target=lambda: out.append(process.stdout.read()), daemon=True), threading.Thread(target=lambda: err.append(process.stderr.read()), daemon=True)]
    for reader in readers:
        reader.start()
    started = time.monotonic()
    while process.poll() is None:
        if ctx.cancelled.is_set() or time.monotonic() - started > timeout:
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, creationflags=_NO_WINDOW)
            process.wait(timeout=5)
            raise ToolError("Cancelled." if ctx.cancelled.is_set() else f"It was still running after {timeout} s, so it was stopped.")
        time.sleep(0.05)
    for reader in readers:
        reader.join(timeout=2)
    return process.returncode, "".join(out), "".join(err)


def _snapshot(folder: Path) -> dict[str, float]:
    found = {}
    for entry in paths.walk_files([folder], max_seconds=1, max_depth=5):
        try:
            found[entry.path] = entry.stat().st_mtime
        except OSError:
            continue
    return found


def _script_source(path: str) -> Path:
    target = resolve_input(path, kinds=(".py",))
    if target.suffix.lower() != ".py":
        raise ToolError(f"{target.name} is not a Python script.", "Use run_command to run other kinds of program.")
    return target


def _gate_python(ctx, code: str = "", timeout: int = 60, path: str = "") -> Approval | None:
    note = "This runs with your user account's access to your files." + (" Network access is blocked." if netguard.is_offline() else "")
    if path and not code:
        try:
            target = _script_source(path)
        except ToolError:
            return None  # the tool itself will report this, and nothing runs
        return Approval(f"Run {target.name}?", note, code=target.read_text(encoding="utf-8", errors="replace")[:3000], language="python", kind="run")
    return Approval("Run this Python script?", note, code=code, language="python", kind="run")


@tool("run_python", "Run Python: either a script saved in a file, by its path, or short code for a job no other tool can do, such as bulk renames or custom data processing. Use path to run a file you just wrote. Print the result. Standard library, PIL, polars, docx and fitz are available. Requires the user's approval.", risk=DANGER, group="Maths & code", gate=_gate_python, doing="Running a script",
      keywords="python script code program automate bulk batch rename every all loop custom process run execute",
      code=arg("string", "A complete Python script that prints its result"), path=arg("string", "Or a saved .py file to run, e.g. desktop/primes.py"), timeout=arg("integer", "Seconds to allow, default 60"))
def run_python(ctx, code: str = "", timeout: int = 60, path: str = "") -> Result:
    if path and not code:
        code = _script_source(path).read_text(encoding="utf-8", errors="replace")
    if not code.strip():
        raise ToolError("There is nothing to run.", "Pass code, or the path of a saved script.")
    workspace = config.WORKSPACE
    workspace.mkdir(parents=True, exist_ok=True)
    script = paths.temp_file(".py")
    prelude = netguard.CHILD_PRELUDE if netguard.is_offline() else ""
    script.write_text(prelude + code, encoding="utf-8")
    before = _snapshot(workspace)
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "MPLBACKEND": "Agg"}
    try:
        code_out, out, err = _run(ctx, [sys.executable.replace("pythonw.exe", "python.exe"), "-X", "utf8", str(script)], cwd=workspace, timeout=max(1, min(int(timeout), 600)), env=env)
    finally:
        script.unlink(missing_ok=True)
    after = _snapshot(workspace)
    made = [Path(path) for path, stamp in after.items() if before.get(path) != stamp]
    if code_out:
        lines = err.strip().splitlines()
        raise ToolError(f"The script failed:\n{chr(10).join(lines[-6:])[:900]}", "Fix the script and try again, or tell the user if it cannot work.")
    body = out.strip() or "(the script printed nothing)"
    return Result("The script ran successfully.", body[:8000], files=made[:12], verified="Exit code 0. Check the printed output for what it actually did.")


def _gate_command(ctx, command: str = "", timeout: int = 60) -> Approval | None:
    return Approval("Run this PowerShell command?", "It runs with your user account's access and is not restricted by offline mode.", code=command, language="powershell", kind="run")


@tool("run_command", "Last resort: run a PowerShell command for system tasks no other tool covers, such as git, winget or ipconfig. Requires the user's approval.", risk=DANGER, group="Maths & code", gate=_gate_command, doing="Running a command",
      keywords="command powershell shell terminal cmd git ipconfig ping winget run execute",
      command=arg("string", "The PowerShell command", required=True), timeout=arg("integer", "Seconds to allow, default 60"))
def run_command(ctx, command: str, timeout: int = 60) -> Result:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    wrapped = "[Console]::OutputEncoding=[Text.Encoding]::UTF8; " + command
    code_out, out, err = _run(ctx, ["powershell", "-NoProfile", "-NonInteractive", "-Command", wrapped], cwd=Path.home(), timeout=max(1, min(int(timeout), 600)), env=env)
    body = (out.strip() + ("\n" + err.strip() if err.strip() else "")).strip() or "(no output)"
    if code_out:
        raise ToolError(f"The command failed (exit {code_out}):\n{body[:1200]}", "Fix it and try again, or tell the user.")
    return Result("The command finished.", body[:8000], verified="Exit code 0.")
