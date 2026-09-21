"""Task: complete a test checkout on Picket staging (Juniper Farm) through the
guest Safari, verifying the $15.30 total. Run:
  GH_HANDS=vnc GH_VNC_HOST=127.0.0.1 GH_VNC_PORT=<tart-vnc-port> \
    python3 run.py --goal-file examples/picket_staging_checkout.py
The VNC password must be in GH_VNC_PASSWORD_FILE (see ~/.config/ghosthands/tart-vnc.env).
Stripe TEST card 4242 4242 4242 4242 is public test data, not a real card."""

GOAL = ("On the Picket staging site (Juniper Farm), add the Fresh Herb Bundle ($5) "
        "to the cart, choose Greater Baltimore delivery ($10), enter ZIP 21210, "
        "pay with Stripe test card 4242 4242 4242 4242 (expiry 12/28, CVC 123), "
        "and place the order. Verify the order total is $15.30 and report the "
        "order ID from the confirmation page.")

MAX_STEPS = 60

GUIDE = """You are in guest Safari on the Picket staging storefront (Juniper Farm).

Direct URL (use action 'navigate'):
http://192.168.64.1:8000/

CHECKOUT FLOW:
 1. On the storefront, find the "Fresh Herb Bundle" product ($5) and add it to the cart.
 2. Go to the cart / checkout.
 3. Choose the "Greater Baltimore" delivery option ($10 delivery fee).
 4. Enter delivery ZIP code: 21210
 5. At payment, use the Stripe TEST card (public test data, not real):
      card number: 4242 4242 4242 4242
      expiry: 12/28
      CVC: 123
 6. Place the order.
 7. VERIFY on the confirmation page: order total must be $15.30
    (subtotal $5 + delivery $10 + fees/tax = $15.30).
 8. REPORT the order ID shown on the confirmation page.

RULES:
 - This is a staging/test site. The card is Stripe's public test card; no real
   money moves.
 - If a step fails, read the screen and retry or correct the input.
 - Do not leave the staging site.
 - The final answer must include the order ID and the total.
"""
