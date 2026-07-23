1. Shows your reference image as a semi-transparent, draggable, resizable
   window so you can line it up exactly with your drawing app's canvas.
2. When you're happy with the alignment, press D. The overlay hides itself
   (so it doesn't intercept the mouse), waits a few seconds for you to
   click into the drawing app, then traces the image with the mouse using
   potrace, mapped onto exactly the area the overlay was covering.

Controls:
  - Drag anywhere on the image        -> move the window
  - Drag the red square (bottom-right) -> resize
  - Scroll wheel / [ and ] keys        -> adjust transparency
  - D key                              -> hide overlay and start drawing
  - Esc key                            -> abort an in-progress draw, OR
                                           quit the app if not drawing
  - S key                              -> save position/size to canvas_config.json

Requires: pip install pynput potracer pyautogui pillow