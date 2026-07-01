import urwid
import threading
import time

class ProgressDialog:
    def __init__(self, on_close_callback):
        self.progress = 0
        self.on_close_callback = on_close_callback
        self.progress_text = urwid.Text("Progress: 0%")
        self.progress_bar = urwid.ProgressBar('pg normal', 'pg complete', current=0, done=100)
        self.cancel_button = urwid.Button("Cancel", on_press=self.cancel)
        self.dialog_widget = urwid.LineBox(
            urwid.Pile([
                self.progress_text,
                urwid.Divider(),
                self.progress_bar,
                urwid.Divider(),
                urwid.Padding(self.cancel_button, align='center', width=('relative', 20))
            ]),
            title="Working..."
        )
        self.running = True
        self._start_progress_thread()

    def _start_progress_thread(self):
        def run():
            while self.running and self.progress < 100:
                time.sleep(0.1)
                self.progress += 1
                urwid.emit_signal(self, "update_progress")
            if self.running:
                urwid.emit_signal(self, "done")

        threading.Thread(target=run, daemon=True).start()

    def cancel(self, button):
        self.running = False
        self.on_close_callback()

    def widget(self):
        return urwid.Overlay(
            top_w=self.dialog_widget,
            bottom_w=urwid.SolidFill(' '),
            align='center', width=('relative', 40),
            valign='middle', height=('relative', 30)
        )

    def update_ui(self):
        self.progress_bar.set_completion(self.progress)
        self.progress_text.set_text(f"Progress: {self.progress}%")

class MainUI:
    def __init__(self):
        self.main_button = urwid.Button("Start Task", on_press=self.show_dialog)
        self.main_widget = urwid.Padding(urwid.Filler(self.main_button, valign='middle'), left=2, right=2)
        self.loop = urwid.MainLoop(self.main_widget, unhandled_input=self.handle_input)

    def show_dialog(self, button):
        self.dialog = ProgressDialog(self.close_dialog)
        urwid.connect_signal(self.dialog, "update_progress", lambda _: self.loop.draw_screen())
        urwid.connect_signal(self.dialog, "done", lambda _: self.close_dialog())
        self.loop.widget = urwid.Overlay(
            self.dialog.widget(),
            self.main_widget,
            align='center', width=('relative', 60),
            valign='middle', height=('relative', 20)
        )
        self._start_ui_update()

    def _start_ui_update(self):
        def update(loop, user_data=None):
            if hasattr(self, "dialog") and self.dialog.running:
                self.dialog.update_ui()
                loop.set_alarm_in(0.1, update)

        self.loop.set_alarm_in(0.1, update)

    def close_dialog(self):
        self.loop.widget = self.main_widget

    def handle_input(self, key):
        if key in ('q', 'Q'):
            raise urwid.ExitMainLoop()

    def run(self):
        self.loop.run()

# Add signal registration
urwid.register_signal(ProgressDialog, ["update_progress", "done"])

if __name__ == '__main__':
    app = MainUI()
    app.run()
