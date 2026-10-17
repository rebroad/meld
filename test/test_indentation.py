import subprocess

from meld.indentation import get_indent_widths


def test_editorconfig_selects_file_specific_indent_widths(tmp_path):
    (tmp_path / ".editorconfig").write_text(
        "root = true\n[*.{h,cc}]\nindent_size = 4\ntab_width = 8\n"
    )

    assert get_indent_widths(tmp_path / "src/example.cc", 2) == (8, 4)
    assert get_indent_widths(tmp_path / "src/example.py", 2) == (2, 2)


def test_editorconfig_overrides_other_format_configs(tmp_path):
    (tmp_path / ".tabsize").write_text("2")
    (tmp_path / ".clang-format").write_text("TabWidth: 6\nIndentWidth: 3\n")
    (tmp_path / ".editorconfig").write_text(
        "root = true\n[*.py]\nindent_size = 4\ntab_width = 8\n"
    )

    assert get_indent_widths(tmp_path / "example.py", 2) == (8, 4)


def test_editorconfig_path_globs_do_not_cross_directory_boundaries(tmp_path):
    (tmp_path / ".editorconfig").write_text(
        "root = true\n[src/*.cc]\nindent_size = 2\ntab_width = 2\n"
    )

    assert get_indent_widths(tmp_path / "src/example.cc", 8) == (2, 2)
    assert get_indent_widths(tmp_path / "src/nested/example.cc", 8) == (8, 8)


def test_editorconfig_indent_size_tab_uses_tab_width(tmp_path):
    (tmp_path / ".editorconfig").write_text(
        "root = true\n[Makefile]\nindent_size = tab\ntab_width = 6\n"
    )

    assert get_indent_widths(tmp_path / "Makefile", 8) == (6, 6)


def test_clang_format_and_tabsize_precede_preferences(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".tabsize").write_text("2")
    (tmp_path / ".clang-format").write_text("TabWidth: 6\nIndentWidth: 3\n")

    assert get_indent_widths(tmp_path / "example.cc", 4) == (6, 3)

    (tmp_path / ".clang-format").unlink()
    assert get_indent_widths(tmp_path / "example.cc", 4) == (2, 2)


def test_preference_is_used_without_format_config(tmp_path):
    assert get_indent_widths(tmp_path / "example.txt", 4) == (4, 4)
