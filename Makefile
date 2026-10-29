.PHONY: test web-test
# The whole suite: no network, no Connect AI account and no psdoom build needed. V=1 lists each test.
test:
	python3 -m unittest discover -s tests -t tests $(if $(V),-v,)

# The browser version, built (web/build.sh) and played end to end in headless Chromium (tests/web/smoke.mjs).
# CHROMIUM=<path> uses a Chromium of your own instead of Playwright's.
web-test:
	web/build.sh
	cd tests/web && npm ci --silent && node smoke.mjs
