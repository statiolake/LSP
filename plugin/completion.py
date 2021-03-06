import mdpopups
import sublime
import sublime_plugin
import webbrowser
from .core.css import css
from .core.logging import debug
from .core.edit import parse_text_edit
from .core.protocol import Request, InsertTextFormat, Range
from .core.registry import LspTextCommand
from .core.typing import Any, List, Dict, Optional, Generator
from .core.views import FORMAT_STRING, FORMAT_MARKUP_CONTENT, minihtml
from .core.views import range_to_region


class LspResolveDocsCommand(LspTextCommand):

    completions = []  # type: List[Dict[str, Any]]

    def run(self, edit: sublime.Edit, index: int, event: Optional[dict] = None) -> None:
        item = self.completions[index]
        detail = self.format_documentation(item.get('detail') or "")
        documentation = self.format_documentation(item.get("documentation") or "")
        # don't show the detail in the cooperate AC popup if it is already shown in the AC details filed.
        self.is_detail_shown = bool(detail)
        minihtml_content = self.get_content(documentation, detail)
        # NOTE: For some reason, ST does not like it when we show a popup from within this run method.
        sublime.set_timeout(lambda: self.show_popup(minihtml_content))

        if not detail or not documentation:
            # To make sure that the detail or documentation fields doesn't exist we need to resove the completion item.
            # If those fields appear after the item is resolved we show them in the popup.
            self.do_resolve(item)

    def format_documentation(self, content: str) -> str:
        return minihtml(self.view, content, allowed_formats=FORMAT_STRING | FORMAT_MARKUP_CONTENT)

    def get_content(self, documentation: str, detail: str) -> str:
        content = ""
        if detail and not self.is_detail_shown:
            content += "<div class='highlight'>{}</div>".format(detail)
        if documentation:
            content += "<div>{}</div>".format(documentation)
        return content

    def show_popup(self, minihtml_content: str) -> None:
        viewport_width = self.view.viewport_extent()[0]
        mdpopups.show_popup(
            self.view,
            minihtml_content,
            flags=sublime.COOPERATE_WITH_AUTO_COMPLETE,
            css=css().popups,
            wrapper_class=css().popups_classname,
            max_width=viewport_width,
            on_navigate=self.on_navigate
        )

    def on_navigate(self, url: str) -> None:
        webbrowser.open(url)

    def do_resolve(self, item: dict) -> None:
        session = self.best_session('completionProvider.resolveProvider')
        if session:
            session.send_request(
                Request.resolveCompletionItem(item),
                lambda res: self.handle_resolve_response(res))

    def handle_resolve_response(self, item: Optional[dict]) -> None:
        detail = ""
        documentation = ""
        if item:
            detail = self.format_documentation(item.get('detail') or "")
            documentation = self.format_documentation(item.get("documentation") or "")
        if not documentation:
            documentation = "<i>No documentation available.</i>"
        minihtml_content = self.get_content(documentation, detail)
        show = self.update_popup if self.view.is_popup_visible() else self.show_popup
        # NOTE: Update/show popups from the main thread, or else the popup might make the AC widget disappear.
        sublime.set_timeout(lambda: show(minihtml_content))

    def update_popup(self, minihtml_content: str) -> None:
        mdpopups.update_popup(
            self.view,
            minihtml_content,
            css=css().popups,
            wrapper_class=css().popups_classname,
        )


class LspCompleteCommand(sublime_plugin.TextCommand):

    def epilogue(self, item: Dict[str, Any]) -> None:
        additional_edits = item.get('additionalTextEdits')
        if additional_edits:
            edits = [parse_text_edit(additional_edit) for additional_edit in additional_edits]
            self.view.run_command("lsp_apply_document_edit", {'changes': edits})
        command = item.get("command")
        if command:
            debug('Running server command "{}" for view {}'.format(command, self.view.id()))
            self.view.run_command("lsp_execute", {"command_name": command})


class LspCompleteInsertTextCommand(LspCompleteCommand):

    def run(self, edit: sublime.Edit, **item: Any) -> None:
        insert_text = item.get("insertText") or item["label"]
        if item.get("insertTextFormat", InsertTextFormat.PlainText) == InsertTextFormat.Snippet:
            self.view.run_command("insert_snippet", {"contents": insert_text})
        else:
            self.view.run_command("insert", {"characters": insert_text})
        self.epilogue(item)


class LspCompleteTextEditCommand(LspCompleteCommand):

    def run(self, edit: sublime.Edit, **item: Any) -> None:
        text_edit = item["textEdit"]
        new_text = text_edit['newText']
        edit_region = range_to_region(Range.from_lsp(text_edit['range']), self.view)
        if item.get("insertTextFormat", InsertTextFormat.PlainText) == InsertTextFormat.Snippet:
            for region in self.translated_regions(edit_region):
                self.view.erase(edit, region)
            self.view.run_command("insert_snippet", {"contents": new_text})
        else:
            for region in self.translated_regions(edit_region):
                # NOTE: Cannot do .replace, because ST will select the replacement.
                self.view.erase(edit, region)
                self.view.insert(edit, region.a, new_text)
        self.epilogue(item)

    def translated_regions(self, edit_region: sublime.Region) -> Generator[sublime.Region, None, None]:
        selection = self.view.sel()
        primary_cursor_position = selection[0].b
        for region in reversed(selection):
            # For each selection region, apply the same removal as for the "primary" region.
            # To do that, translate, or offset, the LSP edit region into the non-"primary" regions.
            # The concept of "primary" is our own, and there is no mention of it in the LSP spec.
            translation = region.b - primary_cursor_position
            translated_edit_region = sublime.Region(edit_region.a + translation, edit_region.b + translation)
            yield translated_edit_region


def resolve(completion_list: sublime.CompletionList, items: List[sublime.CompletionItem], flags: int = 0) -> None:
    # Resolve the promise on the main thread to prevent any sort of data race for _set_target (see sublime_plugin.py).
    sublime.set_timeout(lambda: completion_list.set_completions(items, flags))
