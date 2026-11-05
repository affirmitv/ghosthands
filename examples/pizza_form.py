"""Task: open Safari and fill in the public httpbin.org sample order form (it only echoes what
you send; nothing is ordered). The same file runs on either target:

  python3 run.py --goal-file examples/pizza_form.py                         # this Mac (Pico hands)
  python3 run.py --goal-file examples/pizza_form.py --target terminal-mac   # a terminal-mac guest
"""

GOAL = "In Safari, fill in and submit the sample pizza order form at https://httpbin.org/forms/post."

MAX_STEPS = 40

GUIDE = """If Safari is not the front app, open it: click an empty part of the desktop, key
cmd+shift+a (Finder opens Applications), then double_click the Safari icon in that window.
Then use action 'navigate' to https://httpbin.org/forms/post

Fill the form, one field at a time (click the field, then type on the next turn):
  Customer name:   Ghost Hands
  Telephone:       555-0100
  E-mail address:  hands@example.com
  Pizza Size:      Medium (click its radio button)
  Pizza Toppings:  Bacon and Mushroom (click both checkboxes)
  Delivery instructions:  Leave it at the door

Then click the "Submit order" button. DONE when the page shows a JSON response containing
"custname": "Ghost Hands". This form only echoes the data back; there is no payment and nothing
is ordered, so no checkpoint is needed for the submit.
"""
