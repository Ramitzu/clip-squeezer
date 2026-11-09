# Contributing

Bug reports and small fixes are welcome.

## Setup

Run `setup.bat`, then start the app with `run.bat`.

## Before opening a pull request

```bat
py -3 -m unittest discover -s tests
py -3 ClipSqueezer.py --cli your_clip.mp4 --target 20
```

The tests need neither ffmpeg nor a window. The second command is a real encode
and must finish with a file under the target.

## How the code is laid out

Everything is in `ClipSqueezer.py`.

- `plan_encode` works out bitrate, resolution and frame rate.
- `encode_to_size` runs the two-pass encode and the single retry.
- The encode runs on a worker thread. It sends updates through `App.events`, and
  `App._poll` applies them. Never touch Tk widgets from the worker.

If you change behaviour, update `README.md` too.
