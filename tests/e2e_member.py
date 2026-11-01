"""Browser test of the family journey: phone sign-in, landing page, own-list protection.
Needs: pip install playwright && playwright install chromium.   Run: python tests/e2e_member.py
"""
import os, subprocess, sys, tempfile, time
from playwright.sync_api import sync_playwright

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
env = dict(os.environ, DATA_DIR=tempfile.mkdtemp(), ADMIN_PASSWORD="pw", PORT="8767")
srv = subprocess.Popen([sys.executable, "app.py"], cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(2)
fails = []
def check(ok, label):
    print(("PASS " if ok else "FAIL ") + label)
    if not ok: fails.append(label)
B = "http://localhost:8767"
try:
    with sync_playwright() as p:
        b = p.chromium.launch()
        errors = []
        # organizer sets up three people
        org = b.new_page(); org.on("pageerror", lambda e: errors.append(str(e)))
        org.goto(B + "/admin/login"); org.fill("input[name=password]", "pw"); org.click("button[type=submit]")
        for n, ph in [("Ann", "555 100 0001"), ("Bob", "555 100 0002"), ("Cara", "555 100 0003")]:
            org.fill("#new-name", n); org.fill("form[action$='/admin/people'] input[name=phone]", ph)
            org.keyboard.press("Enter"); org.wait_for_selector(f".pname strong:text-is('{n}')")

        # Ann on her phone
        ctx = b.new_context(viewport={"width": 390, "height": 800})
        ann = ctx.new_page(); ann.on("pageerror", lambda e: errors.append(str(e)))
        ann.goto(B + "/")
        check(ann.locator("input[type=tel]").count() == 1, "signed-out home shows the phone box")
        ann.fill("input[type=tel]", "000 000 0000"); ann.click("text=Continue")
        check("couldn't find that number" in ann.inner_text(".flash"), "unknown number shows a friendly error")
        ann.fill("input[type=tel]", "(555) 100-0001"); ann.click("text=Continue")
        ann.wait_for_selector("h1:has-text('Welcome, Ann')")
        check("Welcome, Ann" in ann.inner_text("h1"), "welcome message uses her name")
        names = ann.locator(".nrow strong").all_inner_texts()
        check(names == ["Ann", "Bob", "Cara"], f"landing lists everyone ({names})")
        check(ann.locator(".nrow.me .go").inner_text().startswith("Edit my list"), "her own row says 'Edit my list'")

        # Ann adds a gift on her editable list
        ann.click(".nrow.me"); ann.wait_for_selector("h1:has-text('My wish list')")
        ann.fill("input.f-title", "Red Scarf"); ann.fill("input.f-price", "$25")
        ann.click("#add-form button[type=submit]"); ann.wait_for_selector(".item .title:text-is('Red Scarf')")
        check(ann.locator(".item .title").first.inner_text() == "Red Scarf", "gift added on her editable list")
        ann_slug = ann.locator("input[readonly]").input_value().rsplit("/", 1)[1]

        # she tries her own public address: bounced back to the editable list
        ann.goto(B + "/list/" + ann_slug)
        check(ann.url.endswith("/mine"), "opening her own public list redirects to her editable list")
        check(ann.locator("text=I bought this").count() == 0 or ann.locator("button:text-is('I bought this')").count() == 0,
              "no bought controls on her own list")

        # Bob signs in and checks off Ann's gift; Ann's side never shows it
        ctx2 = b.new_context(viewport={"width": 390, "height": 800})
        bob = ctx2.new_page(); bob.on("pageerror", lambda e: errors.append(str(e)))
        bob.goto(B + "/"); bob.fill("input[type=tel]", "5551000002"); bob.click("text=Continue")
        bob.wait_for_selector("h1:has-text('Welcome, Bob')")
        bob.click(".nrow:has(strong:text-is('Ann'))")
        bob.wait_for_selector("h1:has-text(\"Ann's wish list\")")
        check(bob.locator("a:has-text('Back to everyone')").count() == 1, "Bob has a way back to everyone's lists")
        bob.click("button:text-is('I bought this')")
        bob.wait_for_selector("text=You bought this")
        ann.goto(B + "/mine")
        check("already bought" not in ann.inner_text("main").lower() and "you bought" not in ann.inner_text("main").lower(),
              "Ann never sees the checkmark on her list")

        # sign out returns to the sign-in box
        bob.goto(B + "/"); bob.click("text=Sign out")
        bob.wait_for_selector("input[type=tel]")
        check(bob.locator("input[type=tel]").count() == 1, "sign out returns to the phone box")
        check(errors == [], f"no JavaScript errors {errors}")
        b.close()
finally:
    srv.terminate()
print("\nFAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)
