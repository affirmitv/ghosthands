"""Task: create the two StreamOn3 Pro subscription products in Google Play Console,
set their prices, and activate their base plans. (Product IDs are public app identifiers,
not secrets.)  Run:  python3 run.py --goal-file examples/streamon3_subscriptions.py"""

GOAL = "Create two auto-renewing subscription products for the StreamOn3 app in Google Play " \
       "Console, set their United States prices, and activate their base plans."

MAX_STEPS = 130

GUIDE = """You are in Google Play Console (Safari) for the app "StreamOn3 HQ".
Create TWO auto-renewing subscriptions. Do PRODUCT 1 fully (created + base plan + price +
ACTIVE), then PRODUCT 2.

Direct URL for the Subscriptions list (use action 'navigate'):
https://play.google.com/console/u/2/developers/8652367124288959223/app/4974565377261105674/subscriptions

PRODUCT 1:
  product ID:   com.affirmi.streamon3.pro.monthly   (PERMANENT - type EXACTLY)
  name:         StreamOn3 Pro (Monthly)
  base plan ID: monthly-autorenew
  type:         Auto-renewing     billing period: Monthly (P1M)
  price:        8.99 USD

PRODUCT 2 (only after product 1 shows an ACTIVE base plan):
  product ID:   com.affirmi.streamon3.pro.annual    (PERMANENT - type EXACTLY)
  name:         StreamOn3 Pro (Annual)
  base plan ID: annual-autorenew
  type:         Auto-renewing     billing period: Yearly (P1Y)
  price:        79.99 USD

TYPICAL SCREEN FLOW:
 1. On the Subscriptions list, click "Create subscription".
 2. Enter the Product ID, then the Name. Click "Create".
 3. On the subscription detail page, under "Base plans", click "Add base plan".
 4. Enter the base plan ID, choose "Auto-renewing", set the billing period.
 5. Click "Set price"/"Set prices"; find United States; enter the price (8.99 or 79.99); Save/Apply.
 6. READ the United States price on screen and confirm it equals the target, THEN Activate the
    base plan and confirm the dialog.
 7. Return to the Subscriptions list; repeat for product 2.

RULES:
 - If a multi-country price table appears, set United States (USD) to the target; leave other
   countries to auto-convert.
 - Do NOT enable free trials or introductory offers.
 - If Create is disabled, or you see a payments-profile / tax / compliance / login / captcha
   wall, or anything not described here, choose 'verify_stop'.
 - Choose 'done' only when BOTH products show an ACTIVE base plan on the Subscriptions list."""
