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
        ann.wait_for_selector("h2:has-text('Welcome, Ann')")
        check("Welcome, Ann" in ann.inner_text("h2"), "welcome message uses her name")
        names = ann.locator(".people .pname").all_inner_texts()
        check([n.replace(" (you)", "") for n in names] == ["Ann", "Bob", "Cara"], f"landing lists everyone ({names})")
        check(ann.locator(".people .you a:has-text('Edit my list')").count() == 1, "her own row has 'Edit my list'")
        link = ann.locator(".people .you input[readonly]").input_value()
        ann_slug = link.rsplit("/", 1)[1]
        check("/list/" in link and ann.locator(".people .you button:has-text('Copy link')").count() == 1,
              "her row shows a copyable link to her public list")
        ann.click(".people .you button:has-text('Copy link')")
        check(ann.locator(".people .you button.btn.small").first.inner_text() == "Copied!", "Copy link button confirms 'Copied!'")
        check(ann.locator("a:has-text('Text link')").count() == 0, "no Text link buttons anywhere")
        check(ann.locator(".people li.other button.copy").count() == 2, "every other family member has a Copy link button")

        # her editable list: simple page, add a gift (no reload), then edit it by just typing
        ann.click(".people .you a:has-text('Edit my list')"); ann.wait_for_selector("h1:has-text('My wish list')")
        check("Your gifts" not in ann.inner_text("main") and ann.locator("input[readonly]").count() == 0,
              "My list page has no 'Your gifts' header or share box")
        ann.evaluate("window.__noReload = true")
        ann.fill("#add-form input.f-title", "Red Scarf"); ann.fill("#add-form input.f-price", "$25")
        ann.click("#add-form button[type=submit]")
        ann.wait_for_selector(".item.editable input[name=title]")
        check(ann.locator(".item.editable input[name=title]").first.input_value() == "Red Scarf", "gift appears as an open, editable entry")
        check(ann.evaluate("window.__noReload === true"), "adding a gift doesn't reload the page")
        check(ann.evaluate("document.activeElement && document.activeElement.id") == "add-url", "link box refocused for the next gift")
        ann.fill("#add-form input.f-title", "Warm Mug"); ann.click("#add-form button[type=submit]")
        ann.wait_for_selector(".item.editable >> nth=1")
        check(ann.locator(".item.editable").count() == 2, "full list shown (two gifts)")

        # edit an old entry just by typing; it saves itself
        t = ann.locator(".item.editable input[name=title]").first
        with ann.expect_response(lambda r: "/edit" in r.url):
            t.fill("Red Wool Scarf")
        check("Saved" in ann.inner_text("#toast"), "typing in an old entry shows Saved")
        # (leaving the title box also saves; wait for the save that actually carries the new price)
        with ann.expect_response(lambda r: "/edit" in r.url and "30" in (r.request.post_data or "")):
            ann.locator(".item.editable input[name=price]").first.fill("$30")
        ann.reload()
        check(ann.locator(".item.editable input[name=title]").first.input_value() == "Red Wool Scarf"
              and ann.locator(".item.editable input[name=price]").first.input_value() == "$30", "edits persisted after reload")
        # an empty name is refused, not saved
        with ann.expect_response(lambda r: "/edit" in r.url):
            ann.locator(".item.editable input[name=title]").nth(1).fill("")
        check(ann.locator("#toast.bad").count() == 1, "clearing a name shows an error instead of saving")
        ann.reload()
        check(ann.locator(".item.editable input[name=title]").nth(1).input_value() == "Warm Mug", "old name kept after the refused edit")
        # remove with confirm
        ann.once("dialog", lambda d: d.accept())
        ann.locator(".item.editable button:has-text('Remove')").nth(1).click()
        ann.wait_for_function("document.querySelectorAll('.item.editable').length === 1")
        check(ann.locator(".item.editable").count() == 1, "removing a gift works in place")

        # she tries her own public address: bounced back to the editable list
        ann.goto(B + "/list/" + ann_slug)
        check(ann.url.endswith("/mine"), "opening her own public list redirects to her editable list")
        check(ann.locator("text=I bought this").count() == 0 or ann.locator("button:text-is('I bought this')").count() == 0,
              "no bought controls on her own list")

        # Bob signs in and checks off Ann's gift; Ann's side never shows it
        ctx2 = b.new_context(viewport={"width": 390, "height": 800})
        bob = ctx2.new_page(); bob.on("pageerror", lambda e: errors.append(str(e)))
        bob.goto(B + "/"); bob.fill("input[type=tel]", "5551000002"); bob.click("text=Continue")
        bob.wait_for_selector("h2:has-text('Welcome, Bob')")
        bob.click("a.person:has(.pname:text-is('Ann'))")
        bob.wait_for_selector("h1:has-text(\"Ann's wish list\")")
        check(bob.locator("a.mast-link").count() == 1, "Bob has a way back to everyone's lists")
        check(bob.locator('.fog-btn').count() == 1, 'wish list starts behind the fog'); bob.click('.fog-btn'); bob.wait_for_timeout(1700)
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
