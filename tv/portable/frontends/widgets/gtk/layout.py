# Miro - an RSS based video player application
# Copyright (C) 2005-2008 Participatory Culture Foundation
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA  02110-1301 USA
#
# In addition, as a special exception, the copyright holders give
# permission to link the code of portions of this program with the OpenSSL
# library.
#
# You must obey the GNU General Public License in all respects for all of
# the code used other than OpenSSL. If you modify file(s) with this
# exception, you may extend this exception to your version of the file(s),
# but you are not obligated to do so. If you do not wish to do so, delete
# this exception statement from your version. If you delete this exception
# statement from all source files in the program, then also delete it here.

"""miro.frontends.widgets.gtk.layout -- Layout widgets.  """

import gtk

from miro.util import Matrix
from miro.frontends.widgets.gtk.base import Widget, Bin

class Box(Widget):
    def __init__(self, spacing=0):
        Widget.__init__(self)
        self.children = set()
        self.set_widget(self.WIDGET_CLASS(spacing=spacing))

    def pack_start(self, widget, expand=False, padding=0):
        self._widget.pack_start(widget._widget, expand, fill=True,
                padding=padding)
        widget._widget.show()
        self.children.add(widget)

    def pack_end(self, widget, expand=False, padding=0):
        self._widget.pack_end(widget._widget, expand, fill=True,
                padding=padding)
        widget._widget.show()
        self.children.add(widget)

    def remove(self, widget):
        widget._widget.hide() # otherwise gtkmozembed gets confused
        self._widget.remove(widget._widget)
        self.children.remove(widget)

    def enable_widget(self):
        for mem in self.children:
            mem.enable_widget()

    def disable_widget(self):
        for mem in self.children:
            mem.disable_widget()

class HBox(Box):
    WIDGET_CLASS = gtk.HBox

class VBox(Box):
    WIDGET_CLASS = gtk.VBox

class Alignment(Bin):
    def __init__(self, xalign=0, yalign=0, xscale=0, yscale=0,
            top_pad=0, bottom_pad=0, left_pad=0, right_pad=0):
        Bin.__init__(self)
        self.set_widget(gtk.Alignment(xalign, yalign, xscale, yscale))
        self.set_padding(top_pad, bottom_pad, left_pad, right_pad)

    def set(self, xalign=0, yalign=0, xscale=0, yscale=0):
        self._widget.set(xalign, yalign, xscale, yscale)

    def set_padding(self, top_pad=0, bottom_pad=0, left_pad=0, right_pad=0):
        self._widget.set_padding(top_pad, bottom_pad, left_pad, right_pad)

class Splitter(Widget):
    def __init__(self):
        """Create a new spliter."""
        Widget.__init__(self)
        self.set_widget(gtk.HPaned())

    def set_left(self, widget):
        """Set the left child widget."""
        self.left = widget
        self._widget.pack1(widget._widget, resize=False, shrink=False)
        widget._widget.show()

    def set_right(self, widget):
        """Set the right child widget.  """
        self.right = widget
        self._widget.pack2(widget._widget, resize=True, shrink=False)
        widget._widget.show()

    def remove_left(self):
        """Remove the left child widget."""
        if self.left is not None:
            self.left._widget.hide() # otherwise gtkmozembed gets confused
            self._widget.remove(self.left._widget)
            self.left = None

    def remove_right(self):
        """Remove the right child widget."""
        if self.right is not None:
            self.right._widget.hide() # otherwise gtkmozembed gets confused
            self._widget.remove(self.right._widget)
            self.right = None

    def set_left_width(self, width):
        self._widget.set_position(width)

    def get_left_width(self):
        return self._widget.get_position()

    def set_right_width(self, width):
        self._widget.set_position(self.width - width)
        # We should take into account the width of the bar, but this seems
        # good enough.

class Table(Widget):
    """Lays out widgets in a table.  It works very similar to the GTK Table
    widget, or an HTML table.
    """
    def __init__(self, rows, columns):
        Widget.__init__(self)
        self.set_widget(gtk.Table(rows, columns, homogeneous=False))
        self.children = Matrix(rows, columns)

    def set_cell(self, widget, row, column):
        """Add a widget to the table."""
        self.children[row, column] = widget
        self._widget.attach(widget._widget, row, row+1, column, column+1)

    def get_cell(self, widget, row, column):
        return self.children[row, column]

    def remove(self, widget):
        widget._widget.hide() # otherwise gtkmozembed gets confused
        self.children.remove(widget)
        self._widget.remove(widget._widget)

    def set_column_spacing(self, spacing):
        self._widget.set_col_spacings(spacing)

    def set_row_spacing(self, spacing):
        self._widget.set_row_spacings(spacing)
