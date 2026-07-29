"""
shortcuts.py — Keyboard shortcut reference window.
"""

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk


def build_shortcuts_window(parent) -> Gtk.ShortcutsWindow:
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<interface>
  <object class="GtkShortcutsWindow" id="shortcuts">
    <property name="modal">1</property>
    <child>
      <object class="GtkShortcutsSection">
        <property name="section-name">shortcuts</property>
        <property name="title">Shortcuts</property>
        <child>
          <object class="GtkShortcutsGroup">
            <property name="title">File</property>
            <child>
              <object class="GtkShortcutsShortcut">
                <property name="accelerator">&lt;Primary&gt;n</property>
                <property name="title">New window</property>
              </object>
            </child>
            <child>
              <object class="GtkShortcutsShortcut">
                <property name="accelerator">&lt;Primary&gt;o</property>
                <property name="title">Open file</property>
              </object>
            </child>
            <child>
              <object class="GtkShortcutsShortcut">
                <property name="accelerator">&lt;Primary&gt;s</property>
                <property name="title">Save</property>
              </object>
            </child>
            <child>
              <object class="GtkShortcutsShortcut">
                <property name="accelerator">&lt;Primary&gt;&lt;Shift&gt;s</property>
                <property name="title">Save as</property>
              </object>
            </child>
            <child>
              <object class="GtkShortcutsShortcut">
                <property name="accelerator">&lt;Primary&gt;&lt;Shift&gt;e</property>
                <property name="title">Export PDF</property>
              </object>
            </child>
          </object>
        </child>
        <child>
          <object class="GtkShortcutsGroup">
            <property name="title">Editing</property>
            <child>
              <object class="GtkShortcutsShortcut">
                <property name="accelerator">&lt;Primary&gt;z</property>
                <property name="title">Undo</property>
              </object>
            </child>
            <child>
              <object class="GtkShortcutsShortcut">
                <property name="accelerator">&lt;Primary&gt;&lt;Shift&gt;z</property>
                <property name="title">Redo</property>
              </object>
            </child>
            <child>
              <object class="GtkShortcutsShortcut">
                <property name="accelerator">&lt;Primary&gt;f</property>
                <property name="title">Find</property>
              </object>
            </child>
            <child>
              <object class="GtkShortcutsShortcut">
                <property name="accelerator">&lt;Primary&gt;h</property>
                <property name="title">Find and replace</property>
              </object>
            </child>
            <child>
              <object class="GtkShortcutsShortcut">
                <property name="accelerator">Escape</property>
                <property name="title">Close find bar</property>
              </object>
            </child>
            <child>
              <object class="GtkShortcutsShortcut">
                <property name="accelerator">&lt;Primary&gt;Return</property>
                <property name="title">Rebuild the PDF</property>
              </object>
            </child>
          </object>
        </child>
        <child>
          <object class="GtkShortcutsGroup">
            <property name="title">Markdown formatting</property>
            <child>
              <object class="GtkShortcutsShortcut">
                <property name="accelerator">&lt;Primary&gt;b</property>
                <property name="title">Bold</property>
              </object>
            </child>
            <child>
              <object class="GtkShortcutsShortcut">
                <property name="accelerator">&lt;Primary&gt;i</property>
                <property name="title">Italic</property>
              </object>
            </child>
            <child>
              <object class="GtkShortcutsShortcut">
                <property name="accelerator">&lt;Primary&gt;k</property>
                <property name="title">Insert link</property>
              </object>
            </child>
          </object>
        </child>
        <child>
          <object class="GtkShortcutsGroup">
            <property name="title">View</property>
            <child>
              <object class="GtkShortcutsShortcut">
                <property name="accelerator">F9</property>
                <property name="title">Toggle slide panel</property>
              </object>
            </child>
            <child>
              <object class="GtkShortcutsShortcut">
                <property name="accelerator">F8</property>
                <property name="title">Toggle live canvas</property>
              </object>
            </child>
            <child>
              <object class="GtkShortcutsShortcut">
                <property name="accelerator">F10</property>
                <property name="title">Toggle theme panel</property>
              </object>
            </child>

          </object>
        </child>
        <child>
          <object class="GtkShortcutsGroup">
            <property name="title">Presenter mode</property>
            <child>
              <object class="GtkShortcutsShortcut">
                <property name="accelerator">&lt;Primary&gt;p</property>
                <property name="title">Present (convert if needed, then present)</property>
              </object>
            </child>
            <child>
              <object class="GtkShortcutsShortcut">
                <property name="accelerator">Right</property>
                <property name="title">Next slide</property>
              </object>
            </child>
            <child>
              <object class="GtkShortcutsShortcut">
                <property name="accelerator">space</property>
                <property name="title">Next slide</property>
              </object>
            </child>
            <child>
              <object class="GtkShortcutsShortcut">
                <property name="accelerator">Left</property>
                <property name="title">Previous slide</property>
              </object>
            </child>
            <child>
              <object class="GtkShortcutsShortcut">
                <property name="accelerator">Escape</property>
                <property name="title">Exit fullscreen / close presenter</property>
              </object>
            </child>
          </object>
        </child>
        <child>
          <object class="GtkShortcutsGroup">
            <property name="title">Help</property>
            <child>
              <object class="GtkShortcutsShortcut">
                <property name="accelerator">F1</property>
                <property name="title">Keyboard shortcuts</property>
              </object>
            </child>
          </object>
        </child>
      </object>
    </child>
  </object>
</interface>"""

    builder = Gtk.Builder.new_from_string(xml, -1)
    win = builder.get_object("shortcuts")
    win.set_transient_for(parent)
    return win
