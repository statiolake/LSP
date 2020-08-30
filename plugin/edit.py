import sublime
import sublime_plugin
from .core.edit import sort_by_application_order, TextEdit
from .core.logging import debug
from .core.typing import List, Dict, Optional, Any, Generator
from contextlib import contextmanager


@contextmanager
def temporary_setting(settings: sublime.Settings, key: str, val: Any) -> Generator[None, None, None]:
    prev_val = None
    has_prev_val = settings.has(key)
    if has_prev_val:
        prev_val = settings.get(key)
    settings.set(key, val)
    yield
    settings.erase(key)
    if has_prev_val and settings.get(key) != prev_val:
        settings.set(key, prev_val)


class _RenameItem:

    __slots__ = ("changes", "callback")

    def __init__(self, changes: List[TextEdit], callback: Callable[[], None]) -> None:
        self.changes = changes
        self.callback = callback


class ApplyEditListener(sublime_plugin.EventListener):

    to_be_renamed = {}  # type: Dict[str, _RenameItem]

    def on_load(self, view: sublime.View) -> None:
        file_name = view.file_name()
        if file_name:
            remove_item = None  # type: Optional[str]
            global to_be_renamed
            for fn, changes in to_be_renamed.items():
                if os.path.samefile(file_name, fn):
                    view.run_command("lsp_apply_document_edit", {"file_name": fn})
                    break


def apply_workspace_edit(
    window: sublime.Window,
    changes: Optional[Dict[str, List[TextEdit]]],
    callback: Callable[[], None]
) -> None:
    if not changes:
        callback()
        return
    file_count = len(changes)

    def do_status_message() -> None:
        if window.is_valid():
            message = "Applied edits to {} file{}".format(file_count, "" if file_count == 1 else "s")
            window.status_message(message)

    views = window.views()
    for view in views:
        file_name = view.file_name()
        if file_name:
            for fn, file_changes in changes.items():
                if os.path.same_file(file_name, fn):
                    found = True
                    view.run_command("lsp_apply_document_edit", file_changes)
                    changes.pop(fn)
                    break
    if changes:
        global to_be_renamed

        def run_callback_when_empty() -> None:
            if not to_be_renamed:
                do_status_message()
                callback()

        for fn, file_changes in changes.items():
            to_be_renamed[fn] = _RenameItem(file_changes, run_callback_when_empty)
        to_be_renamed = changes
        for fn in changes.keys():
            window.open_file(fn)
    else:
        do_status_message()


class LspApplyDocumentEditCommand(sublime_plugin.TextCommand):

    def run(self, edit: Any, changes: Optional[List[TextEdit]] = None, file_name: Optional[str] = None) -> None:
        # Apply the changes in reverse, so that we don't invalidate the range
        # of any change that we haven't applied yet.
        rename_item = None  # type: Optional[_RenameItem]
        if not changes:
            if isinstance(file_name, str):
                global to_be_renamed
                rename_item = to_be_renamed.pop(file_name)
                changes = rename_item.changes
            else:
                return
        with temporary_setting(self.view.settings(), "translate_tabs_to_spaces", False):
            view_version = self.view.change_count()
            last_row, last_col = self.view.rowcol_utf16(self.view.size())
            for start, end, replacement, version in reversed(sort_by_application_order(changes)):
                if version is not None and version != view_version:
                    debug('ignoring edit due to non-matching document version')
                    continue
                region = sublime.Region(self.view.text_point_utf16(*start), self.view.text_point_utf16(*end))
                if start[0] > last_row and replacement[0] != '\n':
                    # Handle when a language server (eg gopls) inserts at a row beyond the document
                    # some editors create the line automatically, sublime needs to have the newline prepended.
                    self.apply_change(region, '\n' + replacement, edit)
                    last_row, last_col = self.view.rowcol(self.view.size())
                else:
                    self.apply_change(region, replacement, edit)
        if rename_item:
            rename_item.callback()

    def apply_change(self, region: sublime.Region, replacement: str, edit: Any) -> None:
        if region.empty():
            self.view.insert(edit, region.a, replacement)
        else:
            if len(replacement) > 0:
                self.view.replace(edit, region, replacement)
            else:
                self.view.erase(edit, region)
