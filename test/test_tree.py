from unittest import TestCase, mock

import gi

gi.require_version("Gdk", "3.0")
gi.require_version("Gtk", "3.0")

from meld import tree


class ChangeNavigationTests(TestCase):
    def make_model(self):
        with mock.patch.object(tree.DiffTreeStore, "_setup_default_styles"):
            model = tree.DiffTreeStore(1, [])
        model.icon_details = [(None, None, None)] * tree.STATE_MAX
        model.text_attributes = [(None, None, None, None)] * tree.STATE_MAX
        return model

    def test_first_and_last_change_paths_skip_unchanged_rows(self):
        model = self.make_model()
        states = (
            tree.STATE_NORMAL,
            tree.STATE_MODIFIED,
            tree.STATE_NOCHANGE,
            tree.STATE_REMOVED,
            tree.STATE_EMPTY,
        )
        for index, state in enumerate(states):
            it = model.append(None)
            model.set_state(it, 0, state, str(index))

        self.assertEqual(model.first_change_path().to_string(), "1")
        self.assertEqual(model.last_change_path().to_string(), "3")

    def test_change_paths_return_none_when_there_are_no_changes(self):
        model = self.make_model()
        for state in (tree.STATE_NORMAL, tree.STATE_NOCHANGE, tree.STATE_EMPTY):
            it = model.append(None)
            model.set_state(it, 0, state, str(state))

        self.assertIsNone(model.first_change_path())
        self.assertIsNone(model.last_change_path())
