# ghosthands: an agent that drives your screen with a $4 microcontroller

Most agents that operate software do it through a side door. Selenium and Playwright reach into the browser's DOM; accessibility tools read the OS widget tree. Both work right up until the target has no DOM to reach, or a detector spots the automation and kills the session. The target might be a native app, an Electron window, a remote desktop, or a login screen that never exposes a DOM at all. And the whole time, an expensive model sits in the loop and reasons about where to click, frame by frame.

ghosthands takes the front door. It looks at the actual screen and moves a real mouse and keyboard over USB, the same way you would. To the operating system it is a person. We open-sourced it today.

## How it is built

Nothing here is exotic. A four-dollar board moves the mouse, and two cheap models tell it where to go.

**Hands.** A Raspberry Pi Pico, about four dollars, flashed with CircuitPython so it enumerates as a USB mouse and keyboard. It takes one JSON command per line over a serial port: move to an absolute coordinate, click, type, press a key. The input reports it sends are byte-identical to a real keyboard's, so nothing in the event stream marks them as synthetic. The firmware and a flashing script are in the repo.

**Eyes.** A shell loop grabs a fresh screenshot to a file whenever the agent asks for one, with an optional continuous mode at about four frames a second. A grounding model, bytedance/ui-tars-1.5-7b, turns a plain-language target into a pixel: give it the screenshot and "the blue Create button, top right" and it returns a coordinate. Grounding reads pixels, so it is indifferent to how the app was built.

**Brain.** A small vision model, z-ai/glm-5.3-flash, looks at the current screenshot along with the goal and the history so far, and returns one next action as JSON. It runs at about seven and a half cents per million input tokens. A full step (look at the screen, decide, locate the target, click) costs about two hundredths of a cent.

The loop is the obvious one: capture, plan, ground, act, repeat. Every step writes a screenshot and a log line. The expensive frontier model that set up the task is out of the loop entirely. The nickels do the driving.

## Why the hardware is the point

You could skip the Pico and inject events in software. Two reasons not to.

The first is that software events carry a signature. SendInput on Windows, XTEST on Linux, CGEvent on macOS, a WebDriver bridge in the browser. Detection systems already watch for every one of them. A report from a hardware HID device carries no such marker, because the bytes it sends are the bytes any keyboard sends. Nothing in the event stream separates the Pico from the keyboard on your desk.

The second is reach. Because the eyes read pixels and the hands speak USB, the same code drives a browser, a native macOS app, a Windows installer, a game, a device sitting behind a KVM. There is no integration to write per target. If a human can see it and click it, ghosthands can too.

## The first real job

Tonight ghosthands filled out a Google Play compliance form.

The task was small and annoying, the kind a person puts off. A new app build had picked up the advertising ID permission through an analytics SDK, and Google will not roll out a release until the matching declaration in the console says yes. The declaration lives four clicks deep under App content, inside a form of radio buttons and checkboxes.

The agent opened the Advertising ID declaration, set the answer to Yes, and gave Analytics as the reason, then saved. Each control was a real click. The grounding model found the button in the screenshot, and the Pico moved the mouse there and pressed. Two cheap models and a four-dollar microcontroller did the paperwork.

The agent does not save a form like this blindly. Before any action that costs money or is hard to undo, it reads the value back off the screen and holds for a person to confirm the screenshot. Here that was one glance and an approval. Everything up to it ran on its own.

## Where it fits

We build a lot of apps at AppSpace, and every one of them drags a tail of console paperwork behind it: store listings and content declarations, then the same set of forms again over on App Store Connect. None of it is hard. It is all just clicking. ghosthands lets a cheap agent do the clicking and hands a person only the clicks that matter, so one operator can run the paperwork for many stores at once.

The repo has the library, the Pico firmware, the hardware notes, and a couple of runnable examples. It is MIT licensed. Bring your own microcontroller.

Repo: github.com/affirmitv/ghosthands
