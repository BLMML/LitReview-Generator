"""Public Word download names derived from the user-defined project title."""

import re

_UNSAFE = re.compile(r'[\x00-\x1f\x7f<>:"/\\|?*]')


def export_docx_filename(project_title: str | None) -> str:
    # Replace separators instead of taking the basename: a slash in a project
    # title must not silently discard half of the user-defined name.
    name = _UNSAFE.sub("_", project_title or "").strip().strip(".")[:120].rstrip(" .")
    return f"{name or '项目'}文献综述.docx"
