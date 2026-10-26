"""A lambda is a symbol of its own (kind ``lambda``): its body is its code.

The calls in the body of a lambda are calls of the lambda, a call of a
lambda is an edge to it, and the conversion of a lambda to a function
pointer takes its address.  libclang 22 gives a lambda no USR, and the
closures of two lambdas of one function with one signature share one, thus
the key holds the place of the lambda.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_CODE = """\
void work(int);
void reg(void (*)(int));
auto file_scope = [](int x) { work(x); };
struct Client {
  int n;
  void start() {
    auto a = [](int x) { work(x); }; auto b = [](int x) { work(x + 1); };
    a(1); b(2);
    reg([](int y) { work(y); }); work(9);
    auto outer = [this]() { auto inner = [](int z) { int k = z; work(k); }; inner(n); };
    outer();
  }
};
void use() { file_scope(5); }
"""


@pytest.fixture(scope="module")
def extracted(tmp_path_factory):
    from fw_context_mcp.indexer.compile_commands import CompilationUnit
    from fw_context_mcp.indexer.symbols import extract_all

    src: Path = tmp_path_factory.mktemp("lam") / "lam.cpp"
    src.write_text(_CODE, encoding="utf-8")
    unit = CompilationUnit(file=src, directory=src.parent, language="cpp", clang_args=["-std=c++17"])
    return extract_all(unit, with_refs=True)


def _names(extracted) -> dict[str, str]:
    return {s.usr: f"{s.qualified_name}@{s.line}:{s.column}" for s in extracted.symbols}


def _edges(extracted, kind: str) -> set[tuple[str, str, int]]:
    names = _names(extracted)
    return {
        (names.get(r.from_usr or "", "?"), names.get(r.to_usr, r.to_usr), r.from_line)
        for r in extracted.references if r.ref_kind == kind
    }


@pytest.mark.libclang
class TestLambdaSymbols:
    def test_each_lambda_is_one_symbol(self, extracted):
        lambdas = sorted(
            (s.qualified_name, s.line, s.column) for s in extracted.symbols if s.kind == "lambda"
        )

        assert lambdas == [
            ("<lambda>", 3, 19),
            ("Client::start::<lambda>", 7, 14),
            ("Client::start::<lambda>", 7, 47),
            ("Client::start::<lambda>", 9, 9),
            ("Client::start::<lambda>", 10, 18),
            ("Client::start::<lambda>::<lambda>", 10, 42),
        ]

    def test_two_lambdas_of_one_signature_have_two_keys(self, extracted):
        keys = [s.usr for s in extracted.symbols if s.kind == "lambda" and s.line == 7]

        assert len(set(keys)) == 2

    def test_the_parent_of_a_lambda_is_the_code_that_holds_it(self, extracted):
        names = _names(extracted)
        parents = {
            f"{s.qualified_name}@{s.line}:{s.column}": names.get(s.parent_usr, s.parent_usr)
            for s in extracted.symbols if s.kind == "lambda"
        }

        assert parents["Client::start::<lambda>::<lambda>@10:42"] == "Client::start::<lambda>@10:18"
        assert parents["Client::start::<lambda>@7:14"].startswith("Client::start@")
        assert parents["<lambda>@3:19"] == ""

    def test_a_local_of_a_lambda_belongs_to_the_lambda(self, extracted):
        names = _names(extracted)
        [local] = [s for s in extracted.symbols if s.name == "k"]

        assert names[local.parent_usr] == "Client::start::<lambda>::<lambda>@10:42"


@pytest.mark.libclang
class TestLambdaEdges:
    def test_a_call_in_the_body_is_a_call_of_the_lambda(self, extracted):
        calls = _edges(extracted, "call")

        assert ("Client::start::<lambda>@7:14", "work@1:6", 7) in calls
        assert ("Client::start::<lambda>::<lambda>@10:42", "work@1:6", 10) in calls
        assert ("<lambda>@3:19", "work@1:6", 3) in calls

    def test_the_code_after_a_lambda_on_its_line_is_not_the_lambda(self, extracted):
        """`reg([](int y) { work(y); }); work(9);`: the second call is start's."""
        calls = _edges(extracted, "call")

        assert any(caller.startswith("Client::start@") and callee == "work@1:6" and line == 9
                   for caller, callee, line in calls), calls

    def test_a_call_of_a_lambda_is_an_edge_to_it(self, extracted):
        calls = _edges(extracted, "call")

        assert any(c.startswith("Client::start@") and t == "Client::start::<lambda>@7:47" and line == 8
                   for c, t, line in calls), calls
        assert ("Client::start::<lambda>@10:18", "Client::start::<lambda>::<lambda>@10:42", 10) in calls
        assert any(c.startswith("use@") and t == "<lambda>@3:19" for c, t, _ in calls), calls

    def test_a_conversion_to_a_function_pointer_takes_the_address(self, extracted):
        """No call edge to the conversion operator of the closure, which is no symbol."""
        names = _names(extracted)
        calls = [r for r in extracted.references if r.ref_kind == "call"]
        indirect = _edges(extracted, "indirect")

        assert not [r for r in calls if r.to_usr not in names and "operator void" in r.to_usr]
        assert any(c.startswith("Client::start@") and t == "Client::start::<lambda>@9:9"
                   for c, t, _ in indirect), indirect


def _extract(tmp_path: Path, code: str):
    from fw_context_mcp.indexer.compile_commands import CompilationUnit
    from fw_context_mcp.indexer.symbols import extract_all

    src = tmp_path / "m.cpp"
    src.write_text(code, encoding="utf-8")
    unit = CompilationUnit(file=src, directory=tmp_path, language="cpp", clang_args=["-std=c++17"])
    return extract_all(unit, with_refs=True)


def _calls(result) -> list[tuple[str, str, int]]:
    names = {s.usr: s.qualified_name for s in result.symbols}
    return sorted(
        (names.get(r.from_usr or "", "?"), names.get(r.to_usr, r.to_usr), r.from_line)
        for r in result.references if r.ref_kind == "call"
    )


@pytest.mark.libclang
class TestLambdasAndMacros:
    def test_a_lambda_in_a_macro_argument_gives_one_row(self, tmp_path: Path):
        """A lambda in a macro argument has an extent of no width: the tree,
        not the place, says which code holds a call."""
        result = _extract(tmp_path, (
            "void work(int);\n"
            "struct Dev { void run(); };\n"
            "#define CALL_TWICE(f) do { auto _l = f; _l(); _l(); } while (0)\n"
            "template <class T> void tf(T& d) { CALL_TWICE([&] { d.run(); work(1); }); }\n"
            "void use(Dev& d) { tf(d); }\n"
        ))

        rows = [row for row in _calls(result) if row[1] == "work"]
        assert rows == [("tf::<lambda>", "work", 4)], rows

    def test_code_after_a_lambda_in_one_expansion_is_not_the_lambda(self, tmp_path: Path):
        result = _extract(tmp_path, (
            "void work(int); void note(); void post(void (*)());\n"
            "#define SCHEDULE(x) do { post([] { work(x); }); note(); } while (0)\n"
            "void a() { SCHEDULE(1); }\n"
        ))

        rows = _calls(result)
        assert ("a", "note", 3) in rows, rows
        assert ("a::<lambda>", "work", 3) in rows, rows

    def test_a_capture_belongs_to_the_code_around_the_lambda(self, tmp_path: Path):
        result = _extract(tmp_path, (
            "int make(); void work(int);\n"
            "void a() { auto l = [p = make()] { work(p); }; l(); }\n"
        ))

        rows = _calls(result)
        # The capture runs where the lambda is made: never a call of it.
        assert not [row for row in rows if row[0] == "a::<lambda>" and row[1] == "make"], rows
        assert ("a::<lambda>", "work", 2) in rows, rows

    def test_a_lambda_written_in_a_macro_body_is_found(self, tmp_path: Path):
        """The place of its closure is the expansion: the walk tells it."""
        result = _extract(tmp_path, (
            "void work(int);\n"
            "#define RUN(x) do { auto l = [] { work(x); }; l(); } while (0)\n"
            "void a() { RUN(3); }\n"
        ))

        rows = _calls(result)
        assert ("a", "a::<lambda>", 3) in rows, rows
        assert ("a::<lambda>", "work", 3) in rows, rows


@pytest.mark.libclang
def test_each_lambda_that_one_template_gets_has_its_edge(tmp_path: Path):
    """The instances `post<lambda 1>` and `post<lambda 2>` have one USR."""
    result = _extract(tmp_path, (
        "void work(int);\n"
        "template <class F> void post(F f) { f(); }\n"
        "void a() { post([] { work(1); }); post([] { work(2); }); }\n"
    ))

    lambdas = {s.usr for s in result.symbols if s.kind == "lambda"}
    called_by_post = {
        r.to_usr for r in result.references
        if r.ref_kind == "call" and r.to_usr in lambdas and r.from_line == 2
    }
    assert len(lambdas) == 2 and called_by_post == lambdas, (lambdas, called_by_post)
