import json
import math
import random
import threading
import time
import tkinter as tk

import potrace
import pyautogui
from PIL import Image, ImageFilter, ImageTk
from pynput import keyboard as pynput_keyboard

IMAGE_PATH = "images/BnW/whole.jpg"
HANDLE_SIZE = 16
MIN_SIZE = 40
CONFIG_PATH = "canvas_config.json"

# ---- Tracing / fidelity settings ----------------------------------------
# Instead of one black/white cutoff, we trace the image at several
# brightness bands (light -> dark) and draw each pass on top of the last.
# This is what lets shading/mid-tones show up instead of just a silhouette.
THRESHOLD_LEVELS = [205, 165, 125, 85]  # light band first, dark band last
TURD_SIZE = 8  # lower = keep smaller details (was 8)
ALPHAMAX = 1  # lower = sharper corners, hugs bitmap more closely
OPTTOLERANCE = 0.1  # lower = tighter curve fit to the traced bitmap
SUPERSAMPLE = 1
DRAW_START_DELAY = 3

# ---- Movement settings ----------------------------------------------------
DRAW_SPEED_PX_PER_SEC = 100.0
TRAVEL_SPEED_PX_PER_SEC = 300.0
SPEED_VARIANCE = 0.15  # +/- 15% random speed per stroke
CORNER_PAUSE_SEC = 0.03
STEP_INTERVAL_SEC = 0.008

# Default overall speed multiplier (1.0 = speeds above, unmodified).
# Can be overridden at runtime in the UI with the +/- keys, or passed
# directly into run_trace(..., speed_multiplier=...).
DEFAULT_SPEED_MULTIPLIER = 0.5
SPEED_MULTIPLIER_STEP = 0.1
SPEED_MULTIPLIER_MIN = 0.1
SPEED_MULTIPLIER_MAX = 5.0

# Human-like imperfection
JITTER_AMPLITUDE_DRAW = 0.2  # px, tremor while actually drawing
JITTER_AMPLITUDE_TRAVEL = 0.07  # px, tiny tremor while moving between strokes
JITTER_SMOOTHING = 0.1  # lower = smoother/slower-varying tremor
MICRO_PAUSE_PROB = 0.01  # chance per step of a tiny hesitation
MICRO_PAUSE_RANGE = (0.02, 0.09)

# How often (seconds) to poll while paused, waiting to be resumed/stopped.
PAUSE_POLL_INTERVAL_SEC = 0.05

pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.0


def ease_in_out(t):
    """Smoothstep easing so strokes accelerate/decelerate like a hand."""
    return t * t * (3 - 2 * t)


class Jitter:
    """Small low-pass-filtered random walk -> looks like hand tremor,
    not white noise."""

    def __init__(self, smoothing=JITTER_SMOOTHING):
        self.x = 0.0
        self.y = 0.0
        self.smoothing = smoothing

    def step(self, amplitude):
        self.x += (random.uniform(-1, 1) - self.x) * self.smoothing
        self.y += (random.uniform(-1, 1) - self.y) * self.smoothing
        return self.x * amplitude, self.y * amplitude


def cubic_bezier_point(p0, p1, p2, p3, t):
    mt = 1 - t
    x = (mt**3 * p0[0] + 3 * mt**2 * t * p1[0] + 3 * mt * t**2 * p2[0] +
         t**3 * p3[0])
    y = (mt**3 * p0[1] + 3 * mt**2 * t * p1[1] + 3 * mt * t**2 * p2[1] +
         t**3 * p3[1])
    return x, y


def _dist(a, b):
    return math.hypot(b[0] - a[0], b[1] - a[1])


def _sample_bezier(p0, p1, p2, p3, to_screen):
    s0, s1, s2, s3 = to_screen(p0), to_screen(p1), to_screen(p2), to_screen(p3)
    length = _dist(s0, s1) + _dist(s1, s2) + _dist(s2, s3)
    num_steps = max(3, int(length / 2.0))
    return [
        cubic_bezier_point(s0, s1, s2, s3, t / num_steps)
        for t in range(1, num_steps + 1)
    ]


def _sample_line(p0, p1, to_screen):
    s0, s1 = to_screen(p0), to_screen(p1)
    length = _dist(s0, s1)
    num_steps = max(2, int(length / 2.0))
    return [(s0[0] + (s1[0] - s0[0]) * t / num_steps,
             s0[1] + (s1[1] - s0[1]) * t / num_steps)
            for t in range(1, num_steps + 1)]


def sort_paths_nearest_neighbour(curves):
    if not curves:
        return []
    remaining = list(curves)
    sorted_curves = [remaining.pop(0)]
    while remaining:
        last_pt = sorted_curves[-1].start_point
        nearest_idx = 0
        min_distance = float("inf")
        for i, curve in enumerate(remaining):
            start = curve.start_point
            d = math.hypot(start.x - last_pt.x, start.y - last_pt.y)
            if d < min_distance:
                min_distance = d
                nearest_idx = i
        sorted_curves.append(remaining.pop(nearest_idx))
    return sorted_curves


def _wait_while_paused(pause_event, stop_event):
    """Blocks here while paused. Returns True if it actually waited
    (was paused), False if it passed straight through."""
    if pause_event is None:
        return False
    paused = False
    while pause_event.is_set() and not stop_event.is_set():
        paused = True
        time.sleep(PAUSE_POLL_INTERVAL_SEC)
    return paused


def smooth_move(start_pt,
                 end_pt,
                 speed,
                 stop_event,
                 jitter=None,
                 jitter_amplitude=0.0,
                 allow_micro_pause=True,
                 pause_event=None):
    """Returns the (x, y) the mouse actually ends at.

    Normally that's just end_pt. But if the pen gets paused mid-move and
    the user manually repositions the mouse (e.g. dragging with the
    button still held down), the *target* itself is shifted by that same
    delta -- so the move continues on from the new position instead of
    pulling back onto the original absolute point. The caller uses the
    returned position to know how far things have shifted so it can keep
    the rest of the trace (all future points) aligned to it."""
    base_speed = speed
    cur_pos = start_pt
    target = end_pt

    while True:
        distance = _dist(cur_pos, target)
        if distance < 1e-3:
            return target

        speed = base_speed * random.uniform(1 - SPEED_VARIANCE, 1 + SPEED_VARIANCE)
        duration = distance / speed
        steps = max(1, int(duration / STEP_INTERVAL_SEC))
        leg_start = cur_pos
        interrupted = False

        for i in range(1, steps + 1):
            if stop_event.is_set():
                return cur_pos

            was_paused = _wait_while_paused(pause_event, stop_event)
            if stop_event.is_set():
                return cur_pos
            if was_paused:
                # Mouse may have been moved manually while paused (e.g. the
                # button was still held down and the user dragged it).
                # Shift the target by the same delta instead of snapping
                # or walking back to the original point -- the rest of
                # this move (and everything after it) now continues from
                # here.
                actual = pyautogui.position()
                dx = actual[0] - cur_pos[0]
                dy = actual[1] - cur_pos[1]
                target = (target[0] + dx, target[1] + dy)
                cur_pos = actual
                interrupted = True
                break

            raw_t = i / steps
            t = ease_in_out(raw_t)
            cur_x = leg_start[0] + (target[0] - leg_start[0]) * t
            cur_y = leg_start[1] + (target[1] - leg_start[1]) * t

            if jitter is not None and jitter_amplitude > 0:
                jx, jy = jitter.step(jitter_amplitude)
                cur_x += jx
                cur_y += jy

            pyautogui.moveTo(int(cur_x), int(cur_y))
            cur_pos = (cur_x, cur_y)

            if allow_micro_pause and random.random() < MICRO_PAUSE_PROB:
                time.sleep(random.uniform(*MICRO_PAUSE_RANGE))

            time.sleep(STEP_INTERVAL_SEC)

        if not interrupted:
            return target
        # else: loop back and recompute a fresh leg from cur_pos -> target


def _trace_level(img_gray, threshold, img_orig_w, img_orig_h):
    """Trace one brightness band into a list of potrace curves (frame
    curves already filtered out)."""
    img_bw = img_gray.point(lambda p: 1 if p < threshold else 0, mode="1")
    bmp = potrace.Bitmap(img_bw)
    path = bmp.trace(turdsize=TURD_SIZE,
                      alphamax=ALPHAMAX,
                      opttolerance=OPTTOLERANCE)

    def is_outer_frame(curve):
        margin = 2.0
        xs = [curve.start_point.x]
        ys = [curve.start_point.y]
        for seg in curve:
            xs.append(seg.end_point.x)
            ys.append(seg.end_point.y)
        min_x, max_x = min(xs), max(xs)
        min_y, max_y = min(ys), max(ys)
        if (min_x <= margin and min_y <= margin
                and max_x >= (img_orig_w - margin)
                and max_y >= (img_orig_h - margin)):
            return True
        return False

    filtered = [c for c in path if not is_outer_frame(c)]
    return sort_paths_nearest_neighbour(filtered)


def run_trace(image_path,
              start_x,
              start_y,
              width,
              height,
              stop_event,
              speed_multiplier=DEFAULT_SPEED_MULTIPLIER,
              pause_event=None):
    """speed_multiplier scales both DRAW_SPEED_PX_PER_SEC and
    TRAVEL_SPEED_PX_PER_SEC. 1.0 = default speed, 2.0 = twice as fast,
    0.5 = half speed, etc.

    pause_event, if provided, is a threading.Event. While it is set,
    all mouse movement (travel and drawing) freezes in place until it
    is cleared again, or stop_event is set."""
    try:
        pyautogui.mouseUp(button="left")
    except Exception:
        pass

    draw_speed = DRAW_SPEED_PX_PER_SEC * speed_multiplier
    travel_speed = TRAVEL_SPEED_PX_PER_SEC * speed_multiplier

    img = Image.open(image_path).convert("L")

    if SUPERSAMPLE > 1:
        img = img.resize((img.width * SUPERSAMPLE, img.height * SUPERSAMPLE),
                          Image.Resampling.LANCZOS)

    # Light denoise only -- heavy median filtering was erasing fine detail.
    img = img.filter(ImageFilter.MedianFilter(size=3))

    img_orig_w, img_orig_h = img.size
    scale_x = width / img_orig_w
    scale_y = height / img_orig_h

    # Accumulates whenever the user manually repositions the mouse during
    # a pause. Every future to_screen() call includes it, so once you drag
    # the pen somewhere else, the rest of the trace (all remaining curves)
    # continues rigidly shifted to line up with your new position instead
    # of snapping/walking back to the original absolute spot.
    offset_x = 0.0
    offset_y = 0.0

    def to_screen(pt):
        if hasattr(pt, 'x'):
            return (start_x + pt.x * scale_x + offset_x,
                     start_y + pt.y * scale_y + offset_y)
        return (start_x + pt[0] * scale_x + offset_x,
                start_y + pt[1] * scale_y + offset_y)

    levels = [
        _trace_level(img, thresh, img_orig_w, img_orig_h)
        for thresh in THRESHOLD_LEVELS
    ]

    aborted = False
    original_pause = pyautogui.PAUSE
    pyautogui.PAUSE = 0
    current_mouse_pos = pyautogui.position()
    travel_jitter = Jitter()
    draw_jitter = Jitter()

    try:
        for level_idx, curves in enumerate(levels):
            if aborted or stop_event.is_set():
                break

            for curve in curves:
                if stop_event.is_set():
                    aborted = True
                    break

                was_paused = _wait_while_paused(pause_event, stop_event)
                if stop_event.is_set():
                    aborted = True
                    break
                if was_paused:
                    actual = pyautogui.position()
                    offset_x += actual[0] - current_mouse_pos[0]
                    offset_y += actual[1] - current_mouse_pos[1]
                    current_mouse_pos = actual

                start_screen_pt = to_screen(curve.start_point)
                final_pos = smooth_move(current_mouse_pos,
                                         start_screen_pt,
                                         travel_speed,
                                         stop_event,
                                         jitter=travel_jitter,
                                         jitter_amplitude=JITTER_AMPLITUDE_TRAVEL,
                                         pause_event=pause_event)
                offset_x += final_pos[0] - start_screen_pt[0]
                offset_y += final_pos[1] - start_screen_pt[1]
                current_mouse_pos = final_pos

                # Occasional pause before starting a stroke, like a person
                # deciding where the next line goes.
                if random.random() < 0.05:
                    time.sleep(random.uniform(0.05, 0.15))

                button_down = False
                try:
                    pyautogui.mouseDown(button="left")
                    button_down = True
                    current_pos = (curve.start_point.x, curve.start_point.y)

                    for segment in curve:
                        if stop_event.is_set():
                            aborted = True
                            break

                        end_pos = (segment.end_point.x, segment.end_point.y)

                        if segment.is_corner:
                            c_pos = (segment.c.x, segment.c.y)
                            pts = _sample_line(current_pos, c_pos, to_screen)
                            pts += _sample_line(c_pos, end_pos, to_screen)
                        else:
                            c1 = (segment.c1.x, segment.c1.y)
                            c2 = (segment.c2.x, segment.c2.y)
                            pts = _sample_bezier(current_pos, c1, c2, end_pos,
                                                  to_screen)

                        for pt in pts:
                            if stop_event.is_set():
                                aborted = True
                                break
                            final_pos = smooth_move(current_mouse_pos,
                                                     pt,
                                                     draw_speed,
                                                     stop_event,
                                                     jitter=draw_jitter,
                                                     jitter_amplitude=JITTER_AMPLITUDE_DRAW,
                                                     pause_event=pause_event)
                            offset_x += final_pos[0] - pt[0]
                            offset_y += final_pos[1] - pt[1]
                            current_mouse_pos = final_pos

                        if segment.is_corner:
                            time.sleep(CORNER_PAUSE_SEC)

                        current_pos = end_pos
                        if aborted:
                            break
                finally:
                    if button_down:
                        pyautogui.mouseUp(button="left")
                        time.sleep(random.uniform(0.008, 0.02))

                if aborted:
                    break
    finally:
        pyautogui.PAUSE = original_pause
        try:
            pyautogui.mouseUp(button="left")
        except Exception:
            pass

    if aborted:
        print("Trace aborted.")
    else:
        print("Trace complete.")


class OverlayApp:

    def __init__(self, root, image_path):
        self.root = root
        self.image_path = image_path
        self.original_image = Image.open(image_path).convert("RGB")

        self.win_x, self.win_y = 200, 200
        self.win_w, self.win_h = self.original_image.size
        max_start = 700
        if max(self.win_w, self.win_h) > max_start:
            ratio = max_start / max(self.win_w, self.win_h)
            self.win_w = int(self.win_w * ratio)
            self.win_h = int(self.win_h * ratio)

        self.alpha = 0.6
        self.drawing = False
        self.stop_event = threading.Event()
        self.pause_event = threading.Event()
        self._hotkey_listener = None
        self.speed_multiplier = DEFAULT_SPEED_MULTIPLIER

        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", self.alpha)
        self.root.geometry(
            f"{self.win_w}x{self.win_h}+{self.win_x}+{self.win_y}")

        self.canvas = tk.Canvas(self.root, highlightthickness=0, bg="black")
        self.canvas.pack(fill="both", expand=True)

        self.tk_image = None
        self._redraw()

        self._drag_offset = None
        self._resizing = False

        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.root.bind("<MouseWheel>", self._on_scroll)
        self.root.bind("<Button-4>", lambda e: self._change_alpha(0.05))
        self.root.bind("<Button-5>", lambda e: self._change_alpha(-0.05))
        self.root.bind("<Key>", self._on_key)
        self.root.focus_force()

    def _redraw(self):
        resized = self.original_image.resize(
            (max(self.win_w, 1), max(self.win_h, 1)), Image.Resampling.LANCZOS)
        self.tk_image = ImageTk.PhotoImage(resized)

        self.canvas.delete("all")
        self.canvas.create_image(0, 0, anchor="nw", image=self.tk_image)

        self.canvas.create_rectangle(self.win_w - HANDLE_SIZE,
                                      self.win_h - HANDLE_SIZE,
                                      self.win_w,
                                      self.win_h,
                                      fill="red",
                                      outline="white",
                                      tags="handle")

        info = (f"{self.win_w}x{self.win_h} @ ({self.win_x},{self.win_y}) | "
                f"speed={self.speed_multiplier:.1f}x (+/- to adjust) | "
                f"D=draw P=pause S=save Esc=quit")
        self.canvas.create_text(6,
                                 6,
                                 anchor="nw",
                                 text=info,
                                 fill="yellow",
                                 font=("Consolas", 10, "bold"))

    def _on_press(self, event):
        if self.drawing:
            return
        if (self.win_w - HANDLE_SIZE <= event.x <= self.win_w
                and self.win_h - HANDLE_SIZE <= event.y <= self.win_h):
            self._resizing = True
        else:
            self._resizing = False
            self._drag_offset = (event.x, event.y)

    def _on_drag(self, event):
        if self.drawing:
            return
        if self._resizing:
            self.win_w = max(MIN_SIZE, event.x)
            self.win_h = max(MIN_SIZE, event.y)
            self.root.geometry(
                f"{self.win_w}x{self.win_h}+{self.win_x}+{self.win_y}")
            self._redraw()
        elif self._drag_offset:
            dx = event.x - self._drag_offset[0]
            dy = event.y - self._drag_offset[1]
            self.win_x += dx
            self.win_y += dy
            self.root.geometry(
                f"{self.win_w}x{self.win_h}+{self.win_x}+{self.win_y}")

    def _on_release(self, event):
        self._resizing = False
        self._drag_offset = None

    def _on_scroll(self, event):
        self._change_alpha(0.05 if event.delta > 0 else -0.05)

    def _change_alpha(self, delta):
        self.alpha = min(1.0, max(0.15, self.alpha + delta))
        self.root.attributes("-alpha", self.alpha)

    def _change_speed(self, delta):
        self.speed_multiplier = round(
            min(SPEED_MULTIPLIER_MAX,
                max(SPEED_MULTIPLIER_MIN, self.speed_multiplier + delta)), 2)
        self._redraw()

    def _toggle_pause(self):
        if not self.drawing:
            return
        if self.pause_event.is_set():
            self.pause_event.clear()
            print("Resumed drawing.")
        else:
            self.pause_event.set()
            print("Paused drawing.")

    def _on_key(self, event):
        key = event.keysym.lower()
        if key in ("escape", "q"):
            if not self.drawing:
                self.root.destroy()
        elif key == "s":
            self._save()
        elif key == "d" and not self.drawing:
            self._start_draw()
        elif key == "p":
            self._toggle_pause()
        elif event.char == "[":
            self._change_alpha(-0.05)
        elif event.char == "]":
            self._change_alpha(0.05)
        elif event.char in ("+", "=") or key in ("plus", "kp_add"):
            self._change_speed(SPEED_MULTIPLIER_STEP)
        elif event.char == "-" or key in ("minus", "kp_subtract"):
            self._change_speed(-SPEED_MULTIPLIER_STEP)

    def _save(self):
        config = {
            "CANVAS_START_X": self.win_x,
            "CANVAS_START_Y": self.win_y,
            "CANVAS_WIDTH": self.win_w,
            "CANVAS_HEIGHT": self.win_h,
            "SPEED_MULTIPLIER": self.speed_multiplier,
        }
        with open(CONFIG_PATH, "w") as f:
            json.dump(config, f, indent=2)

    def _start_draw(self):
        self.drawing = True
        self.stop_event.clear()
        self.pause_event.clear()
        start_x, start_y = self.win_x, self.win_y
        width, height = self.win_w, self.win_h
        speed_multiplier = self.speed_multiplier

        def on_press(key):
            if key == pynput_keyboard.Key.esc:
                self.stop_event.set()
                return False
            try:
                char = key.char
            except AttributeError:
                char = None
            if char and char.lower() == "p":
                self._toggle_pause()

        self._hotkey_listener = pynput_keyboard.Listener(on_press=on_press)
        self._hotkey_listener.start()

        self.root.withdraw()

        def worker():
            time.sleep(DRAW_START_DELAY)
            try:
                run_trace(self.image_path,
                          start_x,
                          start_y,
                          width,
                          height,
                          self.stop_event,
                          speed_multiplier=speed_multiplier,
                          pause_event=self.pause_event)
            except Exception as exc:
                print("Trace failed:", exc)
            finally:
                self.root.after(0, self._finish_draw)

        threading.Thread(target=worker, daemon=True).start()

    def _finish_draw(self):
        self.drawing = False
        self.pause_event.clear()
        if self._hotkey_listener is not None:
            self._hotkey_listener.stop()
            self._hotkey_listener = None
        self.root.deiconify()
        self.root.attributes("-topmost", True)
        self.root.focus_force()


if __name__ == "__main__":
    root = tk.Tk()
    app = OverlayApp(root, IMAGE_PATH)
    root.mainloop()