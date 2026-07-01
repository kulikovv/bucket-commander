import urwid
import os
import time

from enum import Enum

def human_readable_size(size):
    """Convert file size into a human-readable format (e.g., KB, MB)."""
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024

class Command(Enum):
    OPEN = "open"
    EXIT = "exit"
    COPY = "copy"
    MOVE = "move"
    DELETE = "delete"
    RENAME = "rename"
    REFRESH = "refresh"
    GO_PARENT = "go_parent"
    TOGGLE_HIDDEN = "toggle_hidden"
    EXECUTE = "execute"
    SELECT = "select"
    UNSELECT = "unselect"
    SEARCH = "search"
    HELP = "help"

class Keys(Enum):
    ENTER = "enter"

class FileListBox(urwid.ListBox):
    """Custom ListBox that lets the 'enter' key bubble up and enables mouse handling."""
    def mouse_event(self, size, event, button, col, row, focus):
        """Handle mouse click to select a file."""
        if event == 'mouse press':
            if row < len(self.body):
                self.set_focus(row)  # Focus the clicked file

                # Get the actual text from the clicked file's widget (inside the AttrMap)
                widget = self.body[row].base_widget  # This gives us the actual widget inside AttrMap
                selected_file = widget[0].get_text()[0]  # Assuming the name is the first widget (e.g., Text)

                # Handle selection like an "enter" key press
                self.handle_selection(selected_file, selected_file)  # Pass the file name to the handler
            return True
        return super().mouse_event(size, event, button, col, row, focus)

    def handle_selection(self, filename, selected_file):
        """Handle selection logic for the clicked file or directory."""
        if selected_file == "../":  # If ".." (parent directory) is clicked.
            new_path = os.path.abspath(os.path.join(filename, ".."))
        else:
            new_path = os.path.join(filename, selected_file.rstrip("/"))

        if os.path.isdir(new_path):
            if filename == self.left_path:
                self.left_path = new_path
            else:
                self.right_path = new_path
            self.update_files()
        else:
            pass
            #self.footer_message.set_text(f"Selected file: {selected_file}")

class CommandEdit(urwid.Edit):
    signals = ["enter"]

    def keypress(self, size, key):
        if key == "enter":
            urwid.emit_signal(self, "enter", self)  # Pass self as an argument.
            return None
        return super().keypress(size, key)

class Panel:
    def __init__(self, path):
        self.update_path(path)
        # Create list widgets with highlighting.
        self.left_list = urwid.SimpleFocusListWalker(
            [urwid.AttrMap(self.create_file_widget(f), None, focus_map="reversed") for f in self.left_files]
        )
        # Use custom FileListBox so "enter" key isn't consumed, and mouse events are handled.
        self.left_view = FileListBox(self.left_list)
        # Frames around file lists.
        self.left_frame = urwid.AttrMap(
            urwid.LineBox(self.left_view, title=f" Left: {self.left_path} "), "frame_border"
        )

    def get_files(self, path):
        """Return a sorted list of directory entries with size and creation date."""
        try:
            entries = sorted(os.listdir(path))
            entries = [f + "/" if os.path.isdir(os.path.join(path, f)) else f for f in entries]
        except PermissionError:
            entries = ["<Permission Denied>"]

        # Determine if we are not at the root.
        parent = os.path.abspath(os.path.join(path, ".."))
        current = os.path.abspath(path)
        if current != parent:
            # Prepend ".." for going to the parent directory.
            entries.insert(0, "../")

        # Get file info: size and creation date.
        files_info = []
        for entry in entries:
            entry_path = os.path.join(path, entry.rstrip("/"))
            if os.path.isdir(entry_path):
                size = "<DIR>"
                creation_time = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(os.path.getctime(entry_path)))
            else:
                size = human_readable_size(os.path.getsize(entry_path))
                creation_time = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(os.path.getctime(entry_path)))
            files_info.append((entry, size, creation_time))

        return files_info

    def update_path(self, path):
        files = self.get_files(path)
        self.left_list.clear()
        self.left_list.extend(
            [urwid.AttrMap(self.create_file_widget(f), None, focus_map="reversed") for f in files]
        )
        self.left_frame.original_widget.set_title(f" Left: {files} ")

class FileManager:
    def __init__(self):
        self.left_path = os.getcwd()
        self.right_path = os.getcwd()
        self.focus = "left"

        self.left_files = self.get_files(self.left_path)
        self.right_files = self.get_files(self.right_path)

        


        self.columns = urwid.Columns([
            ("weight", 1, self.left_frame),
            ("weight", 1, self.right_frame)
        ])
        # Set initial focus on left panel.
        self.columns.set_focus_column(0)

        # Create command input using our custom CommandEdit.
        self.command_input = CommandEdit("> ")
        urwid.connect_signal(self.command_input, "enter", self.handle_command)

        self.footer_message = urwid.Text("Press Enter to execute command", align="left")
        self.footer = urwid.Pile([
            self.footer_message,
            urwid.AttrMap(self.command_input, "command_line"),
        ])

        self.layout = urwid.Frame(
            header=urwid.AttrMap(urwid.Text("Midnight Commander Clone - Python"), "title"),
            body=self.columns,
            footer=urwid.AttrMap(self.footer, "footer_text")
        )

        self.loop = urwid.MainLoop(self.layout, unhandled_input=self.keypress, palette=[
            ("command_line", "light cyan", "black"),
            ("reversed", "black", "yellow"),
            ("frame_border", "light blue", "black"),
            ("title", "light green", "black"),
            ("footer_text", "light magenta", "black"),
        ])

    def create_file_widget(self, file_info):
        """Create a widget for displaying file name, size, and creation date."""
        name, size, creation_date = file_info
        # Create three columns for name, size, and creation date with a separator line in between.
        name_col = urwid.Text(name)
        size_col = urwid.Text(size)
        date_col = urwid.Text(creation_date)
        
        # Create the layout with columns.
        columns = urwid.Columns([
            ("weight", 3, name_col),
            ("weight", 1, size_col),
            ("weight", 2, date_col)
        ], dividechars=2)  # Add some space between columns
        
        return columns

    def keypress(self, key):
        if key in ("esc", "q"):
            raise urwid.ExitMainLoop()
        elif key == "tab":
            # Switch focus between panels.
            self.focus = "right" if self.focus == "left" else "left"
            self.columns.set_focus_column(1 if self.focus == "right" else 0)
        elif key in ("up", "down"):
            if self.focus == "left":
                pos = self.left_view.get_focus()[1]
                self.left_view.set_focus((pos + (1 if key == "down" else -1)) % len(self.left_list))
            else:
                pos = self.right_view.get_focus()[1]
                self.right_view.set_focus((pos + (1 if key == "down" else -1)) % len(self.right_list))
        elif key == "enter":
            # Handle Enter when focus is on file panels.
            if self.focus == "left":
                selected = self.left_files[self.left_view.get_focus()[1]]
                self.handle_selection(selected, "left")
            else:
                selected = self.right_files[self.right_view.get_focus()[1]]
                self.handle_selection(selected, "right")

    def handle_selection(self, fileinfo, panel):
        filename, filesize, creation = fileinfo
        if panel == "left":
            base_path = self.left_path
        else:
            base_path = self.right_path

        # Handle ".." to go to the parent directory.
        if filename[0] == ".":
            if filename == "../":
                new_path = os.path.abspath(os.path.join(base_path, ".."))
            else:
                new_path = os.path.join(base_path, filename.rstrip("/"))
        else:
            new_path = os.path.join(base_path, filename.rstrip("/"))

        if os.path.isdir(new_path):
            if panel == "left":
                self.left_path = new_path
            else:
                self.right_path = new_path
            self.update_files()
        else:
            self.footer_message.set_text(f"Selected file: {filename}")

    def handle_command(self, widget):
        command = widget.get_edit_text().strip()
        if command:
            self.footer_message.set_text(f"Executed command: {command}")
            widget.set_edit_text("")

    def run(self):
        self.loop.run()

if __name__ == "__main__":
    FileManager().run()
 