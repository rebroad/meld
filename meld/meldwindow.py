# Copyright (C) 2002-2006 Stephen Kennedy <stevek@gnome.org>
# Copyright (C) 2010-2013 Kai Willadsen <kai.willadsen@gmail.com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 2 of the License, or (at
# your option) any later version.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import logging
import os
import time
from typing import Any, Dict, Optional, Sequence

from gi.repository import Gdk, Gio, GLib, Gtk

# Import support module to get all builder-constructed widgets in the namespace
import meld.ui.gladesupport  # noqa: F401
import meld.ui.util
from meld.conf import PROFILE, _
from meld.const import (
    FILE_FILTER_ACTION_FORMAT,
    TEXT_FILTER_ACTION_FORMAT,
    FileComparisonMode,
)
from meld.dirdiff import DirDiff
from meld.filediff import FileDiff
from meld.imagediff import ImageDiff, files_are_images
from meld.melddoc import ComparisonState, MeldDoc
from meld.menuhelpers import replace_menu_section
from meld.misc import guess_if_remote_x11, debug_print, performance_monitor
from meld.newdifftab import NewDiffTab
from meld.recent import RecentType, recent_comparisons
from meld.settings import get_meld_settings
from meld.task import LifoScheduler
from meld.ui.notebooklabel import NotebookLabel
from meld.vcview import VcView
from meld.windowstate import SavedWindowState

log = logging.getLogger(__name__)

# Add debug output with rate limiting
last_debug_output = 0
DEBUG_INTERVAL = 1.0  # seconds between debug messages

def debug_print(message):
    global last_debug_output
    current_time = time.time()
    if current_time - last_debug_output >= DEBUG_INTERVAL:
        log.info(f"[DEBUG] {message}")
        last_debug_output = current_time

@Gtk.Template(resource_path='/org/gnome/meld/ui/appwindow.ui')
class MeldWindow(Gtk.ApplicationWindow):

    __gtype_name__ = 'MeldWindow'

    appvbox = Gtk.Template.Child()
    folder_filter_button = Gtk.Template.Child()
    text_filter_button = Gtk.Template.Child()
    gear_menu_button = Gtk.Template.Child()
    next_conflict_button = Gtk.Template.Child()
    notebook = Gtk.Template.Child()
    previous_conflict_button = Gtk.Template.Child()
    spinner = Gtk.Template.Child()
    vc_filter_button = Gtk.Template.Child()
    view_toolbar = Gtk.Template.Child()

    def __init__(self):
        super().__init__()
        debug_print("Initializing MeldWindow")

        # Manually handle GAction additions
        actions = (
            ("close", self.action_close),
            ("new-tab", self.action_new_tab),
            ("stop", self.action_stop),
        )
        for name, callback in actions:
            action = Gio.SimpleAction.new(name, None)
            action.connect('activate', callback)
            self.add_action(action)

        state_actions = (
            (
                "fullscreen", self.action_fullscreen_change,
                GLib.Variant.new_boolean(False),
            ),
            (
                "gear-menu", None, GLib.Variant.new_boolean(False),
            ),
        )
        for (name, callback, state) in state_actions:
            action = Gio.SimpleAction.new_stateful(name, None, state)
            if callback:
                action.connect('change-state', callback)
            self.add_action(action)

        # Initialise sensitivity for important actions
        self.lookup_action('stop').set_enabled(False)

        # Fake out the spinner on Windows or X11 forwarding. See Gitlab
        # issues #133 and #507.
        if os.name == "nt" or guess_if_remote_x11():
            for attr in ('stop', 'hide', 'show', 'start'):
                setattr(self.spinner, attr, lambda *args: True)

        self.drag_dest_set(
            Gtk.DestDefaults.MOTION | Gtk.DestDefaults.HIGHLIGHT |
            Gtk.DestDefaults.DROP,
            None, Gdk.DragAction.COPY)
        self.drag_dest_add_uri_targets()
        self.connect(
            "drag_data_received", self.on_widget_drag_data_received)

        self.window_state = SavedWindowState()
        self.window_state.bind(self)

        self.should_close = False
        self.idle_hooked = 0
        self.operation_count = 0
        self.operation_times = []

        # Add notebook signal handlers for widget lifecycle tracking
        self.notebook.connect('realize', self.on_notebook_realize)
        self.notebook.connect('map', self.on_notebook_map)
        self.notebook.connect('unmap', self.on_notebook_unmap)
        self.notebook.connect('switch-page', self.on_switch_page)
        self.notebook.connect('page-removed', self.page_removed)
        self.notebook.connect('page-added', self.page_added)

        if PROFILE != '':
            style_context = self.get_style_context()
            style_context.add_class("devel")

        # Add window state monitoring
        self.connect("window-state-event", self.on_window_state_event)
        self.connect("focus-in-event", self.on_window_focus_event)
        self.connect("focus-out-event", self.on_window_focus_event)

        # Initialize scheduler
        self.scheduler = LifoScheduler()
        self.scheduler.connect("runnable", self.on_scheduler_runnable)

        # Set tooltip delay to 0 for immediate display
        self.spinner.set_has_tooltip(True)
        settings = Gtk.Settings.get_default()
        settings.set_property('gtk-tooltip-timeout', 0)

        self.show_all()

    def on_window_state_event(self, window, event):
        debug_print(f"Window state changed: {event.new_window_state}")
        return False

    def on_window_focus_event(self, window, event):
        debug_print(f"Window focus changed: {event.type}")
        return False

    def do_realize(self):
        debug_print("Realizing window")
        Gtk.ApplicationWindow.do_realize(self)

        app = self.get_application()
        if not app:
            debug_print("Application not initialized yet")
            return

        menu = app.get_menu_by_id("gear-menu")
        if menu:
            self.gear_menu_button.set_popover(
                Gtk.Popover.new_from_model(self.gear_menu_button, menu))

            filter_model = app.get_menu_by_id("text-filter-menu")
            if filter_model:
                self.text_filter_button.set_popover(
                    Gtk.Popover.new_from_model(self.text_filter_button, filter_model))

            filter_menu = app.get_menu_by_id("folder-status-filter-menu")
            if filter_menu:
                self.folder_filter_button.set_popover(
                    Gtk.Popover.new_from_model(self.folder_filter_button, filter_menu))

            vc_filter_model = app.get_menu_by_id('vc-status-filter-menu')
            if vc_filter_model:
                self.vc_filter_button.set_popover(
                    Gtk.Popover.new_from_model(self.vc_filter_button, vc_filter_model))

            meld_settings = get_meld_settings()
            self.update_text_filters(meld_settings)
            self.update_filename_filters(meld_settings)
            self.settings_handlers = [
                meld_settings.connect(
                    "text-filters-changed", self.update_text_filters),
                meld_settings.connect(
                    "file-filters-changed", self.update_filename_filters),
            ]

            meld.ui.util.extract_accels_from_menu(menu, app)

    def update_filename_filters(self, settings):
        filter_items_model = Gio.Menu()
        for i, filt in enumerate(settings.file_filters):
            name = FILE_FILTER_ACTION_FORMAT.format(i)
            filter_items_model.append(
                label=filt.label, detailed_action=f'view.{name}')
        section = Gio.MenuItem.new_section(_("Filename"), filter_items_model)
        section.set_attribute([("id", "s", "custom-filter-section")])
        app = self.get_application()
        filter_model = app.get_menu_by_id("folder-status-filter-menu")
        replace_menu_section(filter_model, section)

    def update_text_filters(self, settings):
        filter_items_model = Gio.Menu()
        for i, filt in enumerate(settings.text_filters):
            name = TEXT_FILTER_ACTION_FORMAT.format(i)
            filter_items_model.append(
                label=filt.label, detailed_action=f'view.{name}')
        section = Gio.MenuItem.new_section(None, filter_items_model)
        section.set_attribute([("id", "s", "custom-filter-section")])
        app = self.get_application()
        filter_model = app.get_menu_by_id("text-filter-menu")
        replace_menu_section(filter_model, section)

    def on_widget_drag_data_received(
            self, wid, context, x, y, selection_data, info, time):
        uris = selection_data.get_uris()
        if uris:
            self.open_paths([Gio.File.new_for_uri(uri) for uri in uris])
            return True

    def on_idle(self):
        debug_print("Idle handler triggered")
        start_time = time.time()
        
        # Track what's running
        if hasattr(self.scheduler, '_tasks'):
            debug_print(f"Idle handler pending tasks: {[t.__name__ for t in self.scheduler._tasks]}")
        
        ret = self.scheduler.iteration()
        if ret and isinstance(ret, str):
            timestamp = time.strftime("%H:%M:%S")
            tooltip_text = f"[{timestamp}] {ret}"
            self.spinner.set_tooltip_text(tooltip_text)
            
            # Only log to stderr once per second
            current_time = time.time()
            if not hasattr(self, '_last_progress_time') or current_time - self._last_progress_time >= 1.0:
                log.info(f"Scanning progress: {tooltip_text}")
                self._last_progress_time = current_time
                debug_print(f"Scanning operation details: {ret}")

        pending = self.scheduler.tasks_pending()
        if pending:
            debug_print(f"Idle handler tasks still pending: {pending}")
            self.spinner.start()
            self.idle_hooked = GLib.idle_add(self.on_idle)
        else:
            debug_print("Idle handler no more pending tasks")
            self.spinner.stop()
            self.spinner.set_tooltip_text("")
            self.idle_hooked = None
            if hasattr(self, '_last_progress_time'):
                delattr(self, '_last_progress_time')
            
            # On window close, this idle loop races widget destruction,
            # and so actions may already be gone at this point.
            stop_action = self.lookup_action('stop')
            if stop_action:
                stop_action.set_enabled(False)
        
        debug_print(f"Idle handler took {time.time() - start_time:.3f} seconds")
        return pending

    def on_scheduler_runnable(self, sched):
        debug_print("Scheduler runnable triggered")
        start_time = time.time()
        
        # Track what's running
        if hasattr(sched, '_tasks'):
            debug_print(f"Pending tasks: {[t.__name__ for t in sched._tasks]}")
        
        if not self.idle_hooked:
            self.spinner.show()
            self.spinner.start()
            self.lookup_action('stop').set_enabled(True)
            self.idle_hooked = GLib.idle_add(self.on_idle)
            # Log when scanning starts
            log.info("Scanning operation started")
            debug_print("Scanning operation started")
        
        pending = self.scheduler.tasks_pending()
        if pending:
            debug_print(f"Tasks still pending: {pending}")
        else:
            debug_print("No more pending tasks")
            self.spinner.stop()
            self.spinner.set_tooltip_text("")
            self.idle_hooked = None
            if hasattr(self, '_last_progress_time'):
                delattr(self, '_last_progress_time')
        
        debug_print(f"Scheduler runnable took {time.time() - start_time:.3f} seconds")
        return pending

    @Gtk.Template.Callback()
    def on_delete_event(self, *extra):
        debug_print("Window delete event")
        # Delete pages from right-to-left.  This ensures that if a version
        # control page is open in the far left page, it will be closed last.
        responses = []
        for page in reversed(self.notebook.get_children()):
            self.notebook.set_current_page(self.notebook.page_num(page))
            responses.append(page.on_delete_event())

        have_cancelled_tabs = any(r == Gtk.ResponseType.CANCEL for r in responses)
        have_saving_tabs = any(r == Gtk.ResponseType.APPLY for r in responses)

        # If we have tabs that are not straight OK responses, we cancel the
        # close. Either something has cancelled the close, or we temporarily
        # cancel the close while async saving is happening.
        cancel_delete = have_cancelled_tabs or have_saving_tabs or self.has_pages()
        # If we have only saving and no cancelled tabs, we record that we
        # should close once the other tabs have closed (assuming the state)
        # doesn't otherwise change.
        self.should_close = have_saving_tabs and not have_cancelled_tabs

        return cancel_delete

    def has_pages(self):
        return self.notebook.get_n_pages() > 0

    def handle_current_doc_switch(self, page):
        debug_print("Handling current doc switch")
        start_time = time.time()
        
        if page:
            # Monitor the page's state changes
            if hasattr(page, 'connect'):
                page.connect('state-changed', lambda p, s: debug_print(f"Page state changed to {s}"))
                page.connect('notify::visible', lambda p, ps: debug_print(f"Page visibility changed: {p.get_visible()}"))
                page.connect('notify::sensitive', lambda p, ps: debug_print(f"Page sensitivity changed: {p.get_sensitive()}"))
                
                # Monitor model changes if it's a tree view page
                if hasattr(page, 'get_model'):
                    model = page.get_model()
                    if model and hasattr(model, 'connect'):
                        model.connect('row-inserted', lambda m, path, iter: debug_print(f"Page model row inserted: {path} - Path: {m.value_path(iter, 0) if hasattr(m, 'value_path') else 'unknown'}"))
                        model.connect('row-deleted', lambda m, path: debug_print(f"Page model row deleted: {path}"))
                        model.connect('rows-reordered', lambda m, path, iter, new_order: debug_print(f"Page model rows reordered: {new_order}"))
        
        # Monitor the notebook's state
        self.notebook.connect('notify::visible', lambda n, ps: debug_print(f"Notebook visibility changed: {n.get_visible()}"))
        self.notebook.connect('notify::sensitive', lambda n, ps: debug_print(f"Notebook sensitivity changed: {n.get_sensitive()}"))
        
        debug_print(f"Doc switch handling took {time.time() - start_time:.3f} seconds")

    @Gtk.Template.Callback()
    def on_switch_page(self, notebook, page, which):
        debug_print(f"Switching to page {which}")
        start_time = time.time()
        
        # Track current focus before switch
        focused = self.get_focus()
        if focused:
            debug_print(f"Current focus before switch: {focused.get_name()}")
        
        # Track page state
        if page:
            debug_print(f"Page {which} state before switch: {page.get_state() if hasattr(page, 'get_state') else 'unknown'}")
            debug_print(f"Page {which} visible: {page.get_visible()}")
            debug_print(f"Page {which} sensitive: {page.get_sensitive()}")
            debug_print(f"Page {which} realized: {page.get_realized()}")
            debug_print(f"Page {which} mapped: {page.get_mapped()}")
            
            # Track model state if it's a tree view
            if hasattr(page, 'get_model'):
                model = page.get_model()
                if model:
                    debug_print(f"Page {which} model state: {model.get_n_columns()} columns")
                    if hasattr(model, 'get_iter_first'):
                        iter = model.get_iter_first()
                        if iter:
                            debug_print(f"Page {which} model has data")
                            # Track first few rows
                            count = 0
                            while iter and count < 3:
                                debug_print(f"Page {which} model row {count}: {[model.get_value(iter, i) for i in range(model.get_n_columns())]}")
                                iter = model.iter_next(iter)
                                count += 1
                        else:
                            debug_print(f"Page {which} model is empty")
        
        # Add event monitoring for the page
        if page:
            page.connect('button-press-event', lambda p, e: debug_print(f"Page {which} received button press"))
            page.connect('key-press-event', lambda p, e: debug_print(f"Page {which} received key press"))
            page.connect('grab-focus', lambda p: debug_print(f"Page {which} grabbed focus"))
            page.connect('focus-in-event', lambda p, e: debug_print(f"Page {which} received focus"))
            page.connect('focus-out-event', lambda p, e: debug_print(f"Page {which} lost focus"))
            page.connect('map', lambda p: debug_print(f"Page {which} mapped"))
            page.connect('realize', lambda p: debug_print(f"Page {which} realized"))
            
            # Monitor child widgets
            def monitor_child_widgets(widget):
                if isinstance(widget, Gtk.Widget):
                    debug_print(f"Widget {widget.get_name()} state: visible={widget.get_visible()}, sensitive={widget.get_sensitive()}, realized={widget.get_realized()}, mapped={widget.get_mapped()}")
                    widget.connect('map', lambda w: debug_print(f"Widget {widget.get_name()} mapped"))
                    widget.connect('realize', lambda w: debug_print(f"Widget {widget.get_name()} realized"))
                    widget.connect('grab-focus', lambda w: debug_print(f"Widget {widget.get_name()} grabbed focus"))
                    widget.connect('focus-in-event', lambda w, e: debug_print(f"Widget {widget.get_name()} received focus"))
                    widget.connect('focus-out-event', lambda w, e: debug_print(f"Widget {widget.get_name()} lost focus"))
                    
                    # Track focus chain
                    if hasattr(widget, 'get_focus_chain'):
                        chain = widget.get_focus_chain()
                        if chain:
                            debug_print(f"Widget {widget.get_name()} focus chain: {[w.get_name() for w in chain]}")
                    
                    # Special handling for tree views
                    if isinstance(widget, Gtk.TreeView):
                        debug_print(f"Found tree view: {widget.get_name()}")
                        widget.connect('row-activated', lambda tv, path, col: debug_print(f"Tree view row activated: {path}"))
                        widget.connect('cursor-changed', lambda tv: debug_print("Tree view cursor changed"))
                        if hasattr(widget, 'get_model'):
                            model = widget.get_model()
                            if model:
                                debug_print(f"Tree view model: {model}")
                                if hasattr(model, 'connect'):
                                    model.connect('row-inserted', lambda m, path, iter: debug_print(f"Model row inserted: {path} - Path: {m.value_path(iter, 0) if hasattr(m, 'value_path') else 'unknown'}"))
                                    model.connect('row-deleted', lambda m, path: debug_print(f"Model row deleted: {path}"))
                                    model.connect('rows-reordered', lambda m, path, iter, new_order: debug_print(f"Model rows reordered: {new_order}"))
                
                if hasattr(widget, 'get_children'):
                    for child in widget.get_children():
                        monitor_child_widgets(child)
            
            monitor_child_widgets(page)
        
        # Track focus chain for the notebook
        if hasattr(self.notebook, 'get_focus_chain'):
            chain = self.notebook.get_focus_chain()
            if chain:
                debug_print(f"Notebook focus chain: {[w.get_name() for w in chain]}")
        
        self.handle_current_doc_switch(page)
        
        # Track focus after switch
        focused = self.get_focus()
        if focused:
            debug_print(f"Current focus after switch: {focused.get_name()}")
        
        debug_print(f"Page switch took {time.time() - start_time:.3f} seconds")

    @Gtk.Template.Callback()
    def after_switch_page(self, notebook, page, which):
        newdoc = notebook.get_nth_page(which)
        newdoc.on_container_switch_in_event(self)

    def action_new_tab(self, action, parameter):
        self.append_new_comparison()

    def action_close(self, *extra):
        i = self.notebook.get_current_page()
        if i >= 0:
            page = self.notebook.get_nth_page(i)
            page.on_delete_event()

    def action_fullscreen_change(self, action, state):
        window_state = self.get_window().get_state()
        is_full = window_state & Gdk.WindowState.FULLSCREEN
        action.set_state(state)
        if state and not is_full:
            self.fullscreen()
        elif is_full:
            self.unfullscreen()

    def action_stop(self, *args):
        # TODO: This is the only window-level action we have that still
        # works on the "current" document like this.
        self.current_doc().action_stop()

    def page_removed(self, *args):
        debug_print("Starting page removal")
        start_time = time.time()

        # Handle both signal and direct calls
        if len(args) == 4:  # Signal call: notebook, page, page_num, response
            notebook, page, page_num, response = args
        elif len(args) == 2:  # Direct call: page, response
            page, response = args
            page_num = self.notebook.page_num(page)
        else:
            debug_print(f"Unexpected number of arguments to page_removed: {len(args)}")
            return

        if response == Gtk.ResponseType.CANCEL:
            return

        if response == Gtk.ResponseType.APPLY:
            self.should_close = True
            return

        if hasattr(page, 'scheduler'):
            debug_print("Removing scheduler")
            scheduler_start = time.time()
            self.scheduler.remove_scheduler(page.scheduler)
            debug_print(f"Scheduler removal took {time.time() - scheduler_start:.3f} seconds")

        if self.notebook.get_current_page() == page_num:
            debug_print("Handling current doc switch")
            doc_switch_start = time.time()
            self.handle_current_doc_switch(page)
            debug_print(f"Doc switch handling took {time.time() - doc_switch_start:.3f} seconds")

        debug_print("Removing notebook page")
        notebook_start = time.time()
        self.notebook.remove_page(page_num)
        debug_print(f"Notebook page removal took {time.time() - notebook_start:.3f} seconds")

        # Normal switch-page handlers don't get run for removing the
        # last page from a notebook.
        if not self.has_pages():
            debug_print("Handling last page removal")
            last_page_start = time.time()
            self.on_switch_page(self.notebook, page, -1)
            if self.should_close:
                cancelled = self.emit(
                    'delete-event', Gdk.Event.new(Gdk.EventType.DELETE))
                if not cancelled:
                    self.destroy()
            debug_print(f"Last page handling took {time.time() - last_page_start:.3f} seconds")
        debug_print(f"Total page removal took {time.time() - start_time:.3f} seconds")

    def on_page_state_changed(self, page, old_state, new_state):
        debug_print(f"Page state changed: {old_state} -> {new_state}")
        if self.should_close and old_state == ComparisonState.Closing:
            # Cancel closing if one of our tabs does
            self.should_close = False

    def on_file_changed(self, srcpage, filename):
        debug_print(f"File changed: {filename}")
        for page in self.notebook.get_children():
            if page != srcpage:
                page.on_file_changed(filename)

    @Gtk.Template.Callback()
    def on_open_recent(self, recent_selector, uri):
        try:
            self.append_recent(uri)
        except (IOError, ValueError):
            # FIXME: Need error handling, but no sensible display location
            log.exception(f'Error opening recent file {uri}')

    def _append_page(self, page):
        debug_print("Appending new page")
        start_time = time.time()
        nbl = NotebookLabel(page=page)
        self.notebook.append_page(page, nbl)
        self.notebook.child_set_property(page, 'tab-expand', True)

        # Change focus to the newly created page only if the user is on a
        # DirDiff or VcView page, or if it's a new tab page. This prevents
        # cycling through X pages when X diffs are initiated.
        if isinstance(self.current_doc(), DirDiff) or \
           isinstance(self.current_doc(), VcView) or \
           isinstance(page, NewDiffTab):
            self.notebook.set_current_page(self.notebook.page_num(page))

        if hasattr(page, 'scheduler'):
            self.scheduler.add_scheduler(page.scheduler)
        if isinstance(page, MeldDoc):
            page.file_changed_signal.connect(self.on_file_changed)
            page.create_diff_signal.connect(
                lambda obj, arg, kwargs: self.append_diff(arg, **kwargs))
            page.tab_state_changed.connect(self.on_page_state_changed)
        page.close_signal.connect(self.page_removed)

        self.notebook.set_tab_reorderable(page, True)
        debug_print(f"Page append took {time.time() - start_time:.3f} seconds")

    def append_new_comparison(self):
        debug_print("Creating new comparison")
        doc = NewDiffTab(self)
        self._append_page(doc)
        self.notebook.on_label_changed(doc, _("New comparison"), None)

        def diff_created_cb(doc, newdoc):
            doc.on_delete_event()
            idx = self.notebook.page_num(newdoc)
            self.notebook.set_current_page(idx)

        doc.connect("diff-created", diff_created_cb)
        return doc

    def append_dirdiff(
        self,
        gfiles: Sequence[Optional[Gio.File]],
        auto_compare: bool = False,
    ) -> DirDiff:
        debug_print(f"Appending directory diff: {gfiles}")
        assert len(gfiles) in (1, 2, 3)
        doc = DirDiff(len(gfiles))
        self._append_page(doc)
        gfiles = [f or Gio.File.new_for_path("") for f in gfiles]
        doc.folders = gfiles
        doc.set_locations()
        if auto_compare:
            doc.scheduler.add_task(doc.auto_compare)
        return doc

    def append_filediff(
            self, gfiles, *, encodings=None, merge_output=None, meta=None):
        debug_print(f"Appending file diff: {gfiles}")
        assert len(gfiles) in (1, 2, 3)

        # Check whether to show image window or not.
        if files_are_images(gfiles):
            doc = ImageDiff(len(gfiles))
        else:
            doc = FileDiff(len(gfiles))
        self._append_page(doc)
        doc.set_files(gfiles, encodings)
        if merge_output is not None:
            doc.set_merge_output_file(merge_output)
        if meta is not None:
            doc.set_meta(meta)
        return doc

    def append_filemerge(self, gfiles, merge_output=None):
        debug_print(f"Appending file merge: {gfiles}")
        if len(gfiles) != 3:
            raise ValueError(
                _("Need three files to auto-merge, got: %r") %
                [f.get_parse_name() for f in gfiles])
        doc = FileDiff(
            len(gfiles), comparison_mode=FileComparisonMode.AutoMerge)
        self._append_page(doc)
        doc.set_files(gfiles)
        if merge_output is not None:
            doc.set_merge_output_file(merge_output)
        return doc

    def append_diff(
        self,
        gfiles: Sequence[Optional[Gio.File]],
        auto_compare: bool = False,
        auto_merge: bool = False,
        merge_output: Optional[Gio.File] = None,
        meta: Optional[Dict[str, Any]] = None,
    ):
        debug_print(f"Appending diff: {gfiles}")
        have_directories = False
        have_files = False
        for f in gfiles:
            if not f:
                continue
            file_type = f.query_file_type(Gio.FileQueryInfoFlags.NONE, None)
            if file_type == Gio.FileType.DIRECTORY:
                have_directories = True
            else:
                have_files = True
        if have_directories and have_files:
            raise ValueError(
                _("Cannot compare a mixture of files and directories"))
        elif have_directories:
            return self.append_dirdiff(gfiles, auto_compare)
        elif auto_merge:
            return self.append_filemerge(gfiles, merge_output=merge_output)
        else:
            return self.append_filediff(
                gfiles, merge_output=merge_output, meta=meta)

    def append_vcview(self, location, auto_compare=False):
        debug_print(f"Appending version control view: {location}")
        doc = VcView()
        self._append_page(doc)
        if isinstance(location, (list, tuple)):
            location = location[0]
        if location:
            doc.set_location(location.get_path())
        if auto_compare:
            doc.scheduler.add_task(doc.auto_compare)
        return doc

    def append_recent(self, uri):
        debug_print(f"Appending recent: {uri}")
        comparison_type, gfiles = recent_comparisons.read(uri)
        comparison_method = {
            RecentType.File: self.append_filediff,
            RecentType.Folder: self.append_dirdiff,
            RecentType.Merge: self.append_filemerge,
            RecentType.VersionControl: self.append_vcview,
        }
        tab = comparison_method[comparison_type](gfiles)
        self.notebook.set_current_page(self.notebook.page_num(tab))
        recent_comparisons.add(tab)
        return tab

    def _single_file_open(self, gfile):
        debug_print(f"Opening single file: {gfile}")
        doc = VcView()

        def cleanup():
            self.scheduler.remove_scheduler(doc.scheduler)
        self.scheduler.add_task(cleanup)
        self.scheduler.add_scheduler(doc.scheduler)
        path = gfile.get_path()
        doc.set_location(path)
        doc.create_diff_signal.connect(
            lambda obj, arg, kwargs: self.append_diff(arg, **kwargs))
        doc.run_diff(path)

    def open_paths(self, gfiles, auto_compare=False, auto_merge=False,
                   focus=False):
        debug_print(f"Opening paths: {gfiles}")
        tab = None
        if len(gfiles) == 1:
            gfile = gfiles[0]
            if not gfile.query_exists():
                raise ValueError(_("Cannot compare a non-existent file"))

            if not gfile or (
                gfile.query_file_type(Gio.FileQueryInfoFlags.NONE, None)
                == Gio.FileType.DIRECTORY
            ):
                tab = self.append_vcview(gfile, auto_compare)
            else:
                self._single_file_open(gfile)

        elif len(gfiles) in (2, 3):
            tab = self.append_diff(gfiles, auto_compare=auto_compare,
                                   auto_merge=auto_merge)
        if tab:
            recent_comparisons.add(tab)
            if focus:
                self.notebook.set_current_page(self.notebook.page_num(tab))

        return tab

    def current_doc(self):
        debug_print("Getting current document")
        "Get the current doc or a dummy object if there is no current"
        index = self.notebook.get_current_page()
        if index >= 0:
            page = self.notebook.get_nth_page(index)
            if isinstance(page, MeldDoc):
                return page

        class DummyDoc:
            def __getattr__(self, a):
                return lambda *x: None
        return DummyDoc()

    def on_notebook_realize(self, widget):
        debug_print("Notebook realized")
        for i in range(self.notebook.get_n_pages()):
            page = self.notebook.get_nth_page(i)
            if page:
                page.connect('realize', lambda p, idx=i: self.on_page_realize(p, idx))
                page.connect('map', lambda p, idx=i: self.on_page_map(p, idx))
                page.connect('unmap', lambda p, idx=i: self.on_page_unmap(p, idx))

    def on_notebook_map(self, widget):
        debug_print("Notebook mapped")

    def on_notebook_unmap(self, widget):
        debug_print("Notebook unmapped")

    def on_page_realize(self, page, page_num):
        debug_print(f"Page {page_num} realized")

    def on_page_map(self, page, page_num):
        debug_print(f"Page {page_num} mapped")

    def on_page_unmap(self, page, page_num):
        debug_print(f"Page {page_num} unmapped")

    def page_added(self, notebook, page, page_num):
        debug_print(f"Page {page_num} added")
        page.connect('realize', lambda p, idx=page_num: self.on_page_realize(p, idx))
        page.connect('map', lambda p, idx=page_num: self.on_page_map(p, idx))
        page.connect('unmap', lambda p, idx=page_num: self.on_page_unmap(p, idx))
