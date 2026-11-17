"""Safe parsing, symbolic evidence, and hard resource limits."""

import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from igris import symcheck


@pytest.fixture
def symbolic():
    pytest.importorskip("sympy")


@pytest.mark.parametrize("text", [
    "x" * (symcheck.MAX_LENGTH + 1),
    "(" * 100 + "1" + ")" * 100 + "=1",
    "-" * 200 + "1=1",
    "1+" * 150 + "1=1",
    "2^" * 150 + "2=1",
    "1=__import__('os').system('echo unsafe')",
    "1=eval('1')", "1=exec('1')", "1=sympify('1')",
    "1=unknown(1)", r"\input{private}=1", "1=1 garbage",
    r"\frac{1}{2}=", "x<2", "1==1", "=1", "1=1)",
    r"\begin{pmatrix}1&2&3&4\end{pmatrix}=1",
])
def test_bad_or_excessive_input_is_unreadable_before_any_worker(text, monkeypatch):
    def forbidden():
        pytest.fail("Rejected input must not start a symbolic worker")

    monkeypatch.setattr(symcheck, "available", forbidden)
    start = time.monotonic()
    assert symcheck.verify_line(text)["status"] == "unreadable"
    assert time.monotonic() - start < 0.5


def test_optional_dependency_is_not_required(monkeypatch):
    monkeypatch.setattr(symcheck, "available", lambda: False)
    assert symcheck.verify_line("1=1")["status"] == "unavailable"


@pytest.mark.parametrize("text", [
    r"\frac{1}{2}+\frac{1}{3}=\frac{5}{6}",
    r"\sqrt{x^2}=|x|",
    r"(x+1)^2=x^2+2*x+1=1+x*(x+2)",
    r"\sin(x)^2+\cos(x)^2=1",
    r"\sin^2(x)+\cos^{2}(x)=1",
    r"\ln(\exp(x))=x",
    "(1,2)+(3,4)=(4,6)",
    r"(1,2)\cdot(3,4)=11",
    r"(1,0,0)\times(0,1,0)=(0,0,1)",
    r"\lVert(3,4)\rVert=5",
    r"\det\begin{pmatrix}1&2\\3&4\end{pmatrix}=-2",
    r"\left|\begin{bmatrix}1&2\\3&4\end{bmatrix}\right|=-2",
    r"\begin{vmatrix}1&2&0\\0&1&3\\2&0&1\end{vmatrix}=13",
    r"\begin{bmatrix}1&2\\3&4\end{bmatrix}*\begin{pmatrix}1\\2\end{pmatrix}=(5,11)",
    r"\frac{d}{dx}(x^3)=3*x^2",
    "diff(sin(x),x)=cos(x)",
    r"\int_0^1 x^2 dx=1/3",
    "integrate(x^2,x,0,1)=1/3",
    r"\int 2*x dx=x^2+C",
    "integrate(cos(x),x)=sin(x)+C",
    r"\lim_{x\to 0}\frac{\sin(x)}{x}=1",
    "limit(sin(x)/x,x,0)=1",
])
def test_supported_correct_identities(text, symbolic):
    result = symcheck.verify_line(text)
    assert result["status"] == "true", (text, result)
    assert result["samples"] >= 7


@pytest.mark.parametrize("text, equality", [
    ("2+3=5=6", 2),
    ("(x-1)^2=x^2-2*x+1=x^2+2*x+1", 2),
    (r"\sqrt{x^2}=x", 1),
    (r"(1,0,0)\times(0,1,0)=(0,0,-1)", 1),
    (r"\det\begin{pmatrix}1&2\\3&4\end{pmatrix}=2", 1),
    ("diff(x^3,x)=-3*x^2", 1),
    ("integrate(x,x,0,1)=-1/2", 1),
    ("integrate(2*x,x)=x^2+x+C", 1),
])
def test_false_equalities_have_symbolic_and_numerical_evidence(text, equality, symbolic):
    result = symcheck.verify_line(text)
    assert result["status"] == "false", (text, result)
    assert result["equality"] == equality
    assert result["samples"] >= 7


@pytest.mark.parametrize("text", [
    "1/0=1/0", "0/0=0", "9^99^99=1", "(99^99)^99=1",
    r"\lim_{x\to 0}1/x=0",
    r"\begin{pmatrix}1&2\\3\end{pmatrix}=1",
])
def test_undefined_and_unsupported_expressions_remain_unreadable(text, symbolic):
    assert symcheck.verify_line(text)["status"] == "unreadable"


def test_deadline_kills_and_reaps_child(symbolic, monkeypatch):
    children = []
    killed = []
    original = subprocess.Popen

    def start(command, **kwargs):
        # A genuinely stuck child, independent of import and scheduling speed.
        process = original([*command[:3], "import time; time.sleep(30)", *command[4:]], **kwargs)
        children.append(process)
        kill = process.kill

        def terminate():
            killed.append(process.pid)
            kill()

        monkeypatch.setattr(process, "kill", terminate)
        return process

    monkeypatch.setattr(symcheck.subprocess, "Popen", start)
    monkeypatch.setattr(symcheck, "LINE_TIMEOUT", 0.01)
    started = time.monotonic()
    result = symcheck.verify_lines(["1=1", "2=2", "3=3"])
    assert all(item["status"] == "unreadable" for item in result)
    assert all("time limit" in item["reason"] for item in result)
    assert time.monotonic() - started < 1
    assert len(children) == 1 and killed == [children[0].pid]
    assert children[0].poll() is not None


def test_small_explicit_timeout_includes_startup(symbolic, monkeypatch):
    children = []
    original = subprocess.Popen

    def start(*args, **kwargs):
        process = original(*args, **kwargs)
        children.append(process)
        return process

    monkeypatch.setattr(symcheck.subprocess, "Popen", start)
    started = time.monotonic()
    result = symcheck.verify_line("x+1=x+1", timeout=0.001)
    assert result["status"] == "unreadable" and "time limit" in result["reason"]
    assert time.monotonic() - started < 1
    assert children[0].poll() is not None


def test_numeric_agreement_is_not_a_proof(symbolic):
    import sympy as sp

    # This nonzero polynomial vanishes at every possible sampling point. Spot
    # checks must never turn numerical agreement into an identity certificate.
    x = sp.Symbol("x", real=True)
    roots = {sp.Rational(n, d) for n in (-7, -5, -2, 1, 3, 6, 9) for d in (1, 2, 3)}
    polynomial = sp.prod(x - root for root in roots)
    status, samples = symcheck._compare(polynomial, sp.Integer(0), sp)
    assert status == "unknown"
    assert samples == 7


def test_complex_points_do_not_disprove_an_identity_on_a_real_domain(symbolic):
    result = symcheck.verify_line("ln(x^2)=2*ln(x)")
    assert result["status"] in ("true", "unknown")


@pytest.mark.parametrize("text", ["x=2", "x^2=4"])
def test_context_dependent_solution_equations_are_not_false_identities(text, symbolic):
    assert symcheck.verify_line(text)["status"] == "unknown"


def test_continuations_and_line_budget(monkeypatch):
    seen = []

    def fake(lines, timeout):
        seen.append((lines, timeout))
        return [{"status": "true"} for _ in lines]

    monkeypatch.setattr(symcheck, "_run_lines", fake)
    monkeypatch.setattr(symcheck, "available", lambda: True)
    monkeypatch.setattr(symcheck, "MAX_LINES", 2)
    result = symcheck.verify_lines(["2+3=5", "=5", "1=1"])
    assert seen == [(["2+3=5", "5=5"], 6.0)]
    assert [item["line"] for item in result] == [1, 2, 3]
    assert result[2]["status"] == "unreadable"


def test_submission_budget_is_capped_and_invalid_lines_keep_their_position(monkeypatch):
    seen = []

    def fake(lines, timeout):
        seen.append((lines, timeout))
        return [{"status": "true"} for _ in lines]

    monkeypatch.setattr(symcheck, "_run_lines", fake)
    monkeypatch.setattr(symcheck, "available", lambda: True)
    results = symcheck.verify_lines(["1=1", "unreadable"] + ["2=2"] * 10)
    assert seen == [(["1=1"] + ["2=2"] * 10, 20.0)]
    assert results[1]["status"] == "unreadable" and results[2]["line"] == 3


def test_real_submission_uses_one_isolated_child(symbolic, monkeypatch):
    original = subprocess.Popen
    commands = []

    def start(command, **kwargs):
        commands.append(command)
        assert command[:3] == [sys.executable, "-I", "-c"]
        assert command[4] == str(Path(symcheck.__file__).resolve().parent.parent)
        assert kwargs["creationflags"] == (subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        command = list(command)
        command[3] += "; print('diagnostic'); os.write(1, b'native diagnostic')"
        return original(command, **kwargs)

    monkeypatch.setattr(symcheck.subprocess, "Popen", start)
    results = symcheck.verify_lines([
        r"1\cdot 4+2\cdot(-5)+3\cdot 6=4+10+18", "4-10+18=12", "2+3=5"])
    assert [result["status"] for result in results] == ["false", "true", "true"]
    assert len(commands) == 1
    assert "multiprocessing" not in Path(symcheck.__file__).read_text(encoding="utf-8")


def test_child_independently_rejects_unsafe_raw_text(symbolic, monkeypatch):
    # Bypass only the parent's preflight: the real child must use its own reader.
    monkeypatch.setattr(symcheck, "_trees", lambda text: [])
    assert symcheck.verify_line("1=__import__('os')")["status"] == "unreadable"


@pytest.mark.skipif(os.name != "nt", reason="Windows GUI interpreter")
def test_real_child_supports_pythonw(symbolic, monkeypatch):
    executable = Path(sys.executable).with_name("pythonw.exe")
    if not executable.is_file():
        pytest.skip("pythonw.exe is not installed")
    monkeypatch.setattr(symcheck.sys, "executable", str(executable))
    assert symcheck.verify_line("4-10+18=12")["status"] == "true"


@pytest.mark.parametrize("launch", ["script", "stdin"])
def test_real_child_never_reimports_unguarded_main(symbolic, tmp_path, launch):
    root = str(Path(symcheck.__file__).resolve().parent.parent)
    marker = tmp_path / "main-starts"
    source = fr"""import json, sys
from pathlib import Path
sys.path.insert(0, {root!r})
marker = Path({str(marker)!r})
with marker.open('a') as output:
    output.write('started\\n')
assert marker.read_text().count('started') == 1, 'main was re-imported'
from igris.symcheck import verify_lines
print(json.dumps(verify_lines([r'1\cdot 4+2\cdot(-5)+3\cdot 6=4+10+18', '4-10+18=12'])))
"""
    if launch == "script":
        script = tmp_path / "unguarded.py"
        script.write_text(source, encoding="utf-8")
        command, data = [sys.executable, str(script)], ""
    else:
        command, data = [sys.executable, "-"], source
    completed = subprocess.run(command, input=data, text=True, capture_output=True, timeout=15)
    assert completed.returncode == 0, completed.stderr
    assert [item["status"] for item in json.loads(completed.stdout)] == ["false", "true"]
    assert marker.read_text().count("started") == 1


@pytest.mark.parametrize("output,code", [(b"not json", 0), (b"[]", 0),
                                        (b'[{}]', 0), (b'[{"status":[]}]', 0), (b"[]", 1)])
def test_child_failures_are_unreadable(monkeypatch, output, code):
    class FailedProcess:
        returncode = code

        def communicate(self, *args, **kwargs):
            return output, None

        def poll(self):
            return self.returncode

    monkeypatch.setattr(symcheck, "available", lambda: True)
    monkeypatch.setattr(symcheck.subprocess, "Popen", lambda *args, **kwargs: FailedProcess())
    assert symcheck.verify_line("1=1")["status"] == "unreadable"
