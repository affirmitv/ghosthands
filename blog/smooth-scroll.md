# Making the hands scroll like a thumb

The hands could already move, click, and type like a person. The last motion was the scroll, and a scroll is easy to get almost right and hard to get right.

A mouse wheel speaks in notches. The simple way to scroll a page is to send a few big notches and let the operating system sort out the rest. It works, and it reads as a machine every time. The page lurches, overshoots, and settles. A person does not scroll like that. A thumb rolls the wheel in a smooth arc, quick through the middle and slow at the ends, and the page tracks the motion.

So we send it the way a thumb rolls it. Each notch becomes a run of single-unit wheel reports on an ease-in and ease-out cadence, with a little jitter, starting slow, quickening, easing to a stop. The page glides. macOS layers its own acceleration on top, keyed to how fast the reports arrive, so the same run paced slower lands softer. That gave us one dial for feel.

The dial is live. A scroll command takes an optional pace: above one is slower and gentler, below one is quicker. It is set per command, with no re-flash. Travel per notch is a second dial, and both sit at the top of the firmware, one edit away for anyone who wants a different feel.

We tuned it against the Instagram feed, a custom scroll container that shrugs off a lot of synthetic input. It scrolls a post at a time, at whatever speed you ask for.

The hands are meant to be indistinguishable from a person at the keyboard. Scroll was the last motion that still gave the game away. It doesn't anymore.

The code is in the repo, MIT licensed.

Repo: github.com/affirmitv/ghosthands
