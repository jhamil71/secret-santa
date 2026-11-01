"""Browser test of the organizer page (needs: pip install playwright && playwright install chromium).
Run:  python tests/e2e_browser.py
"""
import os, re, subprocess, sys, tempfile, time
from playwright.sync_api import sync_playwright

data = tempfile.mkdtemp()
env = dict(os.environ, DATA_DIR=data, ADMIN_PASSWORD="pw", PORT="8765")
srv = subprocess.Popen([sys.executable, "app.py"], cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))), env=env,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(2)
fails = []
def check(ok, label):
    print(("PASS " if ok else "FAIL ") + label)
    if not ok: fails.append(label)
try:
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 390, "height": 800})  # phone-sized
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.goto("http://localhost:8765/admin/login")
        pg.fill("input[name=password]", "pw"); pg.click("button[type=submit]")
        pg.wait_for_selector("text=Organizer dashboard")

        # mark the document so a full page reload would be detectable
        pg.evaluate("window.__noReload = true")

        # add people rapidly: focus returns to the name box, page doesn't reload
        for n, ph in [("Ann", "555 100 0001"), ("Bob", "555 100 0002"), ("Cara", "555 100 0003"), ("Dan", "555 100 0004")]:
            pg.fill("#new-name", n); pg.fill("form[action$='/admin/people'] input[name=phone]", ph); pg.keyboard.press("Enter")
            pg.wait_for_selector(f".pname strong:text-is('{n}')")
        check(pg.evaluate("window.__noReload === true"), "adding people doesn't reload the page")
        check(pg.evaluate("document.activeElement && document.activeElement.id") == "new-name", "name box refocused after adding")
        check(pg.locator(".prow").count() == 4, "four people listed")
        check("Added Dan" in pg.inner_text("#toast"), "toast confirms the add")
        check(pg.locator(".flash").count() == 0, "no banner pushing the page down")

        # settings autosave: type, pause, no Save button needed
        pg.fill("input[name=budget]", "$40")
        pg.wait_for_function("document.getElementById('toast').textContent.includes('Saved')")
        pg.fill("input[name=event_note]", "Dec 25 at Grandma's"); pg.keyboard.press("Tab")
        time.sleep(1.2)
        check(pg.locator("form[data-autosave] button:visible").count() == 0, "no Save buttons visible")
        pg.reload(); pg.wait_for_selector("text=Organizer dashboard")
        check(pg.input_value("input[name=budget]") == "$40" and "Grandma" in pg.input_value("input[name=event_note]"),
              "budget and note persisted after reload")
        pg.evaluate("window.__noReload = true")

        # checkbox autosaves immediately
        pg.uncheck("input[name=avoid_swaps]")
        pg.wait_for_function("document.getElementById('toast').textContent.includes('Saved')")
        pg.reload(); pg.wait_for_selector("text=Organizer dashboard")
        check(not pg.is_checked("input[name=avoid_swaps]"), "checkbox change persisted")
        pg.check("input[name=avoid_swaps]"); time.sleep(0.6); pg.evaluate("window.__noReload = true")

        # couple rule: adds in place
        pg.select_option("form[action$='/admin/exclusions'] select[name=a]", label="Ann")
        pg.select_option("form[action$='/admin/exclusions'] select[name=b]", label="Bob")
        pg.click("form[action$='/admin/exclusions'] button[type=submit]")
        pg.wait_for_selector("text=can't draw each other")
        check(pg.evaluate("window.__noReload === true"), "adding a rule doesn't reload")

        # last year's matches autosave when you pick
        sel = pg.locator("form[action$='/admin/history'] select").first
        with pg.expect_response(lambda r: r.url.endswith("/admin/history")):
            sel.select_option(label="Cara")
        check("Saved" in pg.inner_text("#toast"), "toast shows Saved after picking last year's match")
        pg.reload(); pg.wait_for_selector("text=Organizer dashboard")
        check(pg.locator("form[action$='/admin/history'] select").first.evaluate("e => e.options[e.selectedIndex].text") == "Cara",
              "last year's pick persisted")
        pg.evaluate("window.__noReload = true")

        # edit a person's name: autosaves, other places rename live, panel stays open
        pg.click("#pedit-1 summary")
        with pg.expect_response(lambda r: r.url.endswith("/admin/people/1/edit")):
            pg.fill("#pedit-1 input[name=name]", "Annie")
        time.sleep(0.2)
        check(pg.locator("[data-pname='1']").first.inner_text() == "Annie", "name updates in the list instantly")
        check(pg.locator("option[data-pid='1']").first.inner_text() == "Annie", "name updates in the dropdowns instantly")
        check(pg.locator("#pedit-1").evaluate("e => e.open"), "edit panel stays open")
        # bad phone shows an error and doesn't save
        pg.fill("#pedit-1 input[name=phone]", "abc")
        pg.wait_for_function("document.getElementById('toast').classList.contains('bad')")
        check("phone" in pg.inner_text("#toast").lower(), "bad phone number shows an error toast")
        pg.fill("#pedit-1 input[name=phone]", "555 100 0001")
        pg.wait_for_function("!document.getElementById('toast').classList.contains('bad')")

        # draw in place; confirm dialog still honored
        pg.once("dialog", lambda d: d.dismiss())
        pg.click("text=🎲 Draw names"); time.sleep(0.5)
        check(pg.locator("text=Drawn").count() == 0, "cancelling the confirm dialog does not draw")
        pg.once("dialog", lambda d: d.accept())
        pg.click("text=🎲 Draw names")
        pg.wait_for_selector(".pill.green:has-text('Drawn')")
        check(pg.evaluate("window.__noReload === true"), "drawing doesn't reload the page")
        check("Names drawn" in pg.inner_text("#toast"), "toast confirms the draw")

        check(errors == [], f"no JavaScript errors {errors}")
        b.close()
finally:
    srv.terminate()
print("\nFAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)
