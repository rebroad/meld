# Copyright (C) 2026 The Meld authors
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 2 of the License, or
# (at your option) any later version.

import os
import re
import subprocess


def _expand_editorconfig_braces(pattern):
    start = pattern.find("{")
    if start < 0:
        return [pattern]
    depth = 0
    for end in range(start, len(pattern)):
        if pattern[end] == "{":
            depth += 1
        elif pattern[end] == "}":
            depth -= 1
            if depth == 0:
                break
    if depth:
        return [pattern]
    body = pattern[start + 1 : end]
    depth = 0
    parts = []
    part_start = 0
    for index, char in enumerate(body):
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
        elif char == "," and depth == 0:
            parts.append(body[part_start:index])
            part_start = index + 1
    parts.append(body[part_start:])
    if len(parts) == 1:
        range_match = re.fullmatch(r"(-?\d+)\.\.(-?\d+)", body)
        if not range_match:
            return [pattern]
        first, last = map(int, range_match.groups())
        if first >= last or last - first > 1000:
            return [pattern]
        parts = [str(number) for number in range(first, last + 1)]
    return [
        expanded
        for part in parts
        for expanded in _expand_editorconfig_braces(
            pattern[:start] + part + pattern[end + 1 :]
        )
    ]


def _editorconfig_glob_matches(pattern, path):
    if "/" not in pattern:
        pattern = "**/" + pattern
    pattern = pattern.lstrip("/")
    regex = []
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "\\" and index + 1 < len(pattern):
            index += 1
            regex.append(re.escape(pattern[index]))
        elif char == "*":
            if index + 1 < len(pattern) and pattern[index + 1] == "*":
                index += 1
                if index + 1 < len(pattern) and pattern[index + 1] == "/":
                    index += 1
                    regex.append("(?:.*/)?")
                else:
                    regex.append(".*")
            else:
                regex.append("[^/]*")
        elif char == "?":
            regex.append("[^/]")
        elif char == "[":
            close = pattern.find("]", index + 1)
            if close < 0:
                regex.append(r"\[")
            else:
                contents = pattern[index + 1 : close]
                if contents.startswith("!"):
                    contents = "^" + contents[1:]
                regex.append("[" + contents + "]")
                index = close
        else:
            regex.append(re.escape(char))
        index += 1
    try:
        return re.fullmatch("".join(regex), path) is not None
    except re.error:
        return False


def _editorconfig_values(path):
    directory = os.path.dirname(path)
    configs = []
    while True:
        config_path = os.path.join(directory, ".editorconfig")
        try:
            with open(config_path, encoding="utf-8") as config:
                content = config.read()
        except OSError:
            content = None
        if content is not None:
            configs.append((directory, content))
            if re.search(r"(?im)^\s*root\s*=\s*true\s*(?:#.*)?$", content):
                break
        parent = os.path.dirname(directory)
        if parent == directory:
            break
        directory = parent

    values = {}
    for config_dir, content in reversed(configs):
        section = None
        relative_path = os.path.relpath(path, config_dir).replace(os.sep, "/")
        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith(("#", ";")):
                continue
            if line.startswith("[") and line.endswith("]"):
                section = line[1:-1].strip()
                continue
            if section is None or "=" not in line:
                continue
            key, value = (part.strip() for part in line.split("=", 1))
            key = key.lower()
            value = value.split("#", 1)[0].strip().lower()
            if key not in ("tab_width", "indent_size"):
                continue
            if any(
                _editorconfig_glob_matches(pattern, relative_path)
                for pattern in _expand_editorconfig_braces(section)
            ):
                if value == "unset":
                    values.pop(key, None)
                else:
                    values[key] = value
    return values


def _positive_int(value):
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


def _clang_format_values(path):
    directory = os.path.dirname(path)
    while True:
        config_path = next(
            (
                os.path.join(directory, name)
                for name in (".clang-format", "_clang-format")
                if os.path.isfile(os.path.join(directory, name))
            ),
            None,
        )
        if config_path:
            try:
                with open(config_path, encoding="utf-8") as config:
                    content = config.read()
            except OSError:
                return {}
            values = {}
            for key in ("TabWidth", "IndentWidth"):
                match = re.search(
                    rf"(?m)^\s*{key}:\s*[\"']?(\d+)[\"']?\s*(?:#.*)?$",
                    content,
                )
                if match:
                    values[key.lower()] = _positive_int(match.group(1))
            if values or not re.search(
                r"(?m)^\s*(?:BasedOnStyle:\s*[\"']?InheritParentConfig|InheritParentConfig:\s*true)\b",
                content,
            ):
                return values
        parent = os.path.dirname(directory)
        if parent == directory:
            return {}
        directory = parent


def get_indent_widths(path, preference):
    """Return (tab width, indent width), using file configs before preferences."""
    path = os.path.abspath(os.fspath(path))
    editorconfig = _editorconfig_values(path)
    clang_format = _clang_format_values(path)

    tab_width = _positive_int(editorconfig.get("tab_width"))
    indent_value = editorconfig.get("indent_size")
    indent_width = None if indent_value == "tab" else _positive_int(indent_value)
    if tab_width is None and indent_width is not None:
        tab_width = indent_width
    if indent_value is None and tab_width is not None:
        indent_width = tab_width

    tab_width = tab_width or clang_format.get("tabwidth")
    indent_width = indent_width or clang_format.get("indentwidth")

    tabsize = None
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=os.path.dirname(path),
            capture_output=True,
            text=True,
            check=True,
            timeout=2,
        )
        tabsize_path = os.path.join(result.stdout.strip(), ".tabsize")
        with open(tabsize_path, encoding="ascii") as f:
            tabsize = _positive_int(f.read().strip())
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        pass

    tab_width = tab_width or tabsize or preference
    indent_width = indent_width or tabsize or preference
    if indent_value == "tab":
        indent_width = tab_width
    return tab_width, indent_width
